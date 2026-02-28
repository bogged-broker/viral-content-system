import pandas as pd
import numpy as np
from typing import Dict, List, Optional, Tuple, Any
from pathlib import Path
from datetime import datetime, timedelta
import json
from dataclasses import dataclass, asdict
from collections import defaultdict
from abc import ABC, abstractmethod
import warnings
import asyncio
import math
import logging

# Import real long-tail tracker for integration
try:
    from long_tail_tracker import LongTailTracker
    LONG_TAIL_AVAILABLE = True
except ImportError:
    print("[FactoryMetrics] Long-tail tracker not available, using fallback simulation")
    LONG_TAIL_AVAILABLE = False

warnings.filterwarnings('ignore')

# ===== RAZOR-SHARP ENHANCEMENTS =====

@dataclass
class KPIContract:
    """Hard guarantees for KPI compliance with automatic enforcement."""
    name: str
    min_value: float
    max_value: Optional[float]
    severity: str  # 'warn', 'error', 'halt'
    description: str
    category: str  # 'engagement', 'retention', 'velocity', 'baseline'
    deadline_days: Optional[int] = None  # For time-based contracts
    auto_action: Optional[str] = None  # Auto-action on violation
    
    def validate(self, value: float, context: Dict = None) -> Dict:
        """Validate KPI against contract and return enforcement action."""
        context = context or {}
        
        # Handle None values
        if value is None:
            return {
                'contract_name': self.name,
                'compliant': False,
                'value': None,
                'min_value': self.min_value,
                'max_value': self.max_value,
                'violation_severity': 1.0,
                'action_required': f"KPI value is None",
                'enforcement_action': 'LOG_WARNING',
                'alert_level': 'MEDIUM',
                'category': self.category,
                'context': context
            }
        
        violation = False
        violation_severity = 0.0
        action_required = None
        
        # Check minimum value
        if self.min_value is not None and value < self.min_value:
            violation = True
            violation_severity = (self.min_value - value) / self.min_value
            action_required = f"Value {value} below minimum {self.min_value}"
        
        # Check maximum value
        if self.max_value is not None and value > self.max_value:
            violation = True
            violation_severity = (value - self.max_value) / self.max_value
            action_required = f"Value {value} above maximum {self.max_value}"
        
        # Determine enforcement action
        if violation:
            if self.severity == 'halt':
                action = 'HALT_PRODUCTION'
                alert_level = 'CRITICAL'
            elif self.severity == 'error':
                action = self.auto_action or 'IMMEDIATE_INTERVENTION'
                alert_level = 'HIGH'
            else:  # warn
                action = 'LOG_WARNING'
                alert_level = 'MEDIUM'
        else:
            action = 'COMPLIANT'
            alert_level = 'INFO'
        
        return {
            'contract_name': self.name,
            'compliant': not violation,
            'value': value,
            'min_value': self.min_value,
            'max_value': self.max_value,
            'violation_severity': violation_severity,
            'action_required': action_required,
            'enforcement_action': action,
            'alert_level': alert_level,
            'category': self.category,
            'context': context
        }


@dataclass
class KPIContractViolation:
    """Structured violation reporting for factory governance."""
    contract: KPIContract
    actual_value: float
    expected_value: float
    violation_severity: float
    timestamp: datetime
    video_id: Optional[str] = None
    factory_id: Optional[str] = None
    auto_action_taken: Optional[str] = None
    escalation_triggered: bool = False
    resolution_deadline: Optional[datetime] = None


class KPIContractManager:
    """Manages and enforces KPI contracts across the factory."""
    
    def __init__(self):
        self.contracts: Dict[str, KPIContract] = {}
        self.violations: List[KPIContractViolation] = []
        self.enforcement_log: List[Dict] = []
        self.logger = logging.getLogger('KPIContracts')
        
        # Initialize default contracts
        self._initialize_default_contracts()
    
    def _initialize_default_contracts(self):
        """Initialize razor-sharp default contracts."""
        
        # Engagement contracts
        self.add_contract(KPIContract(
            name='engagement_rate_floor',
            min_value=0.03,  # 3% minimum engagement
            max_value=None,
            severity='error',
            description='Minimum engagement rate to prevent content decay',
            category='engagement',
            auto_action='boost_engagement'
        ))
        
        self.add_contract(KPIContract(
            name='engagement_rate_ceiling',
            min_value=None,
            max_value=0.15,  # 15% maximum (potential bot activity)
            severity='warn',
            description='Maximum engagement rate to detect potential fraud',
            category='engagement',
            auto_action='fraud_review'
        ))
        
        # Retention contracts
        self.add_contract(KPIContract(
            name='retention_rate_floor',
            min_value=0.35,  # 35% minimum retention
            max_value=None,
            severity='error',
            description='Minimum retention rate for content quality',
            category='retention',
            auto_action='content_quality_review'
        ))
        
        # Velocity contracts
        self.add_contract(KPIContract(
            name='velocity_minimum',
            min_value=500,  # 500 views/day minimum
            max_value=None,
            severity='warn',
            description='Minimum daily velocity to prevent stagnation',
            category='velocity',
            auto_action='velocity_boost'
        ))
        
        self.add_contract(KPIContract(
            name='velocity_deadline',
            min_value=16666.67,  # 5M views / 30 days
            max_value=None,
            severity='halt',
            description='Baseline velocity deadline (5M views in 30 days)',
            category='baseline',
            deadline_days=30,
            auto_action='emergency_intervention'
        ))
        
        # Baseline contracts
        self.add_contract(KPIContract(
            name='baseline_compliance',
            min_value=0.8,  # 80% baseline compliance rate
            max_value=None,
            severity='halt',
            description='Factory-wide baseline compliance rate',
            category='baseline',
            auto_action='factory_health_emergency'
        ))
        
        # Quality contracts
        self.add_contract(KPIContract(
            name='virality_minimum',
            min_value=25.0,  # Minimum virality score
            max_value=None,
            severity='warn',
            description='Minimum virality score for content distribution',
            category='engagement',
            auto_action='content_optimization'
        ))
    
    def add_contract(self, contract: KPIContract):
        """Add a new KPI contract."""
        self.contracts[contract.name] = contract
        self.logger.info(f"Added KPI contract: {contract.name}")
    
    def validate_kpi(self, contract_name: str, value: float, context: Dict = None) -> Dict:
        """Validate a single KPI against its contract."""
        if contract_name not in self.contracts:
            return {'error': f'Contract {contract_name} not found'}
        
        contract = self.contracts[contract_name]
        result = contract.validate(value, context)
        
        # Log violation if any
        if not result['compliant']:
            self._log_violation(contract, result, context)
        
        return result
    
    def validate_all_kpis(self, kpis: Dict, video_id: str = None, factory_id: str = None) -> Dict:
        """Validate all KPIs against their contracts."""
        results = {}
        critical_violations = []
        
        for contract_name, contract in self.contracts.items():
            if contract_name in kpis:
                value = kpis[contract_name]
                context = {'video_id': video_id, 'factory_id': factory_id}
                result = self.validate_kpi(contract_name, value, context)
                results[contract_name] = result
                
                # Track critical violations
                if result['alert_level'] == 'CRITICAL':
                    critical_violations.append(result)
        
        # Auto-enforcement for critical violations
        if critical_violations:
            self._auto_enforce_critical_violations(critical_violations, video_id, factory_id)
        
        return {
            'overall_compliant': len([r for r in results.values() if r['compliant']]) == len(results),
            'violations': [r for r in results.values() if not r['compliant']],
            'critical_violations': critical_violations,
            'total_contracts': len(self.contracts),
            'compliance_rate': len([r for r in results.values() if r['compliant']]) / len(results) if results else 0
        }
    
    def _log_violation(self, contract: KPIContract, result: Dict, context: Dict = None):
        """Log KPI contract violation."""
        violation = KPIContractViolation(
            contract=contract,
            actual_value=result['value'],
            expected_value=contract.min_value,
            violation_severity=result['violation_severity'],
            timestamp=datetime.now(),
            video_id=context.get('video_id') if context else None,
            factory_id=context.get('factory_id') if context else None,
            auto_action_taken=result['enforcement_action'],
            escalation_triggered=result['alert_level'] == 'CRITICAL'
        )
        
        self.violations.append(violation)
        
        # Log to enforcement log
        self.enforcement_log.append({
            'timestamp': violation.timestamp.isoformat(),
            'contract': contract.name,
            'severity': contract.severity,
            'action': result['enforcement_action'],
            'video_id': violation.video_id,
            'factory_id': violation.factory_id
        })
        
        self.logger.warning(f"KPI Contract Violation: {contract.name} - {result['action_required']}")
    
    def _auto_enforce_critical_violations(self, violations: List[Dict], video_id: str, factory_id: str):
        """Auto-enforce critical violations."""
        for violation in violations:
            action = violation['enforcement_action']
            
            if action == 'HALT_PRODUCTION':
                self._trigger_production_halt(violation, video_id, factory_id)
            elif action == 'IMMEDIATE_INTERVENTION':
                self._trigger_immediate_intervention(violation, video_id, factory_id)
            elif action == 'EMERGENCY_INTERVENTION':
                self._trigger_emergency_intervention(violation, video_id, factory_id)
    
    def _trigger_production_halt(self, violation: Dict, video_id: str, factory_id: str):
        """Trigger production halt for critical violations."""
        self.logger.critical(f" PRODUCTION HALT TRIGGERED: {violation['contract_name']} - {violation['action_required']}")
        # In production, this would trigger actual production halt
        pass
    
    def _trigger_immediate_intervention(self, violation: Dict, video_id: str, factory_id: str):
        """Trigger immediate intervention."""
        self.logger.error(f" IMMEDIATE INTERVENTION: {violation['contract_name']} - {violation['action_required']}")
        # In production, this would trigger immediate intervention
        pass
    
    def _trigger_emergency_intervention(self, violation: Dict, video_id: str, factory_id: str):
        """Trigger emergency intervention."""
        self.logger.critical(f" EMERGENCY INTERVENTION: {violation['contract_name']} - {violation['action_required']}")
        # In production, this would trigger emergency intervention
        pass
    
    def get_compliance_report(self) -> Dict:
        """Generate comprehensive compliance report."""
        if not self.violations:
            return {
                'total_violations': 0,
                'compliance_rate': 1.0,
                'critical_violations': 0,
                'status': 'COMPLIANT'
            }
        
        total_violations = len(self.violations)
        critical_violations = len([v for v in self.violations if v.contract.severity == 'halt'])
        
        return {
            'total_violations': total_violations,
            'compliance_rate': 1.0 - (total_violations / (total_violations + len(self.contracts))),
            'critical_violations': critical_violations,
            'status': 'CRITICAL' if critical_violations > 0 else 'WARNING' if total_violations > 0 else 'COMPLIANT',
            'violations_by_category': self._group_violations_by_category(),
            'recent_violations': [v for v in self.violations if (datetime.now() - v.timestamp).days <= 7]
        }
    
    def _group_violations_by_category(self) -> Dict:
        """Group violations by category."""
        categories = defaultdict(list)
        for violation in self.violations:
            categories[violation.contract.category].append(violation)
        return {cat: len(violations) for cat, violations in categories.items()}


# 🔥 2. Real-Time Anomaly Detection System

@dataclass
class AnomalyDetectionResult:
    """Result of anomaly detection with confidence and recommendations."""
    anomaly_type: str  # 'spike', 'drop', 'trend_break', 'outlier'
    severity: str  # 'low', 'medium', 'high', 'critical'
    confidence: float  # 0-1 confidence in anomaly
    affected_metrics: List[str]  # Which metrics are affected
    detected_at: datetime
    expected_range: Tuple[float, float]  # Expected min/max values
    actual_values: List[float]  # Actual anomalous values
    recommendations: List[str]  # Recommended actions
    auto_action_triggered: bool  # Whether automatic action was taken


class RealTimeAnomalyDetector:
    """Real-time anomaly detection for factory metrics."""
    
    def __init__(self, sensitivity: float = 0.95, lookback_window: int = 30):
        self.sensitivity = sensitivity  # Statistical confidence threshold
        self.lookback_window = lookback_window  # Days to look back for baseline
        self.logger = logging.getLogger('AnomalyDetector')
        
        # Anomaly detection thresholds
        self.thresholds = {
            'spike_detection': 3.0,  # 3 standard deviations
            'drop_detection': -2.5,  # 2.5 standard deviations negative
            'trend_break': 0.3,  # 30% trend change
            'outlier': 2.0  # 2 standard deviations from mean
        }
        
        # Historical baselines
        self.metric_baselines: Dict[str, Dict] = {}
        
    def detect_anomalies(self, current_metrics: Dict, historical_data: pd.DataFrame) -> List[AnomalyDetectionResult]:
        """
        Detect anomalies in current metrics compared to historical baselines.
        
        Args:
            current_metrics: Current factory metrics
            historical_data: Historical metrics data for baseline
            
        Returns:
            List[AnomalyDetectionResult]: Detected anomalies with recommendations
        """
        anomalies = []
        
        # Update baselines from historical data
        self._update_baselines(historical_data)
        
        # Check each metric for anomalies
        for metric_name, current_value in current_metrics.items():
            if metric_name in self.metric_baselines:
                baseline = self.metric_baselines[metric_name]
                
                # Spike detection
                spike_anomaly = self._detect_spike(metric_name, current_value, baseline)
                if spike_anomaly:
                    anomalies.append(spike_anomaly)
                
                # Drop detection
                drop_anomaly = self._detect_drop(metric_name, current_value, baseline)
                if drop_anomaly:
                    anomalies.append(drop_anomaly)
                
                # Outlier detection
                outlier_anomaly = self._detect_outlier(metric_name, current_value, baseline)
                if outlier_anomaly:
                    anomalies.append(outlier_anomaly)
        
        # Auto-trigger actions for critical anomalies
        for anomaly in anomalies:
            if anomaly.severity == 'critical':
                self._auto_trigger_anomaly_response(anomaly)
        
        return anomalies
    
    def _update_baselines(self, historical_data: pd.DataFrame):
        """Update statistical baselines from historical data."""
        if historical_data.empty:
            return
        
        numeric_columns = historical_data.select_dtypes(include=[np.number]).columns
        
        for col in numeric_columns:
            if col in historical_data.columns:
                values = historical_data[col].dropna()
                if len(values) > 10:  # Need sufficient data points
                    self.metric_baselines[col] = {
                        'mean': values.mean(),
                        'std': values.std(),
                        'median': values.median(),
                        'q25': values.quantile(0.25),
                        'q75': values.quantile(0.75),
                        'min': values.min(),
                        'max': values.max(),
                        'count': len(values)
                    }
    
    def _detect_spike(self, metric_name: str, current_value: float, baseline: Dict) -> Optional[AnomalyDetectionResult]:
        """Detect sudden spikes in metrics."""
        if baseline['std'] == 0:
            return None
        
        z_score = (current_value - baseline['mean']) / baseline['std']
        
        if z_score > self.thresholds['spike_detection']:
            severity = self._calculate_severity(z_score, 'spike')
            confidence = min(1.0, abs(z_score) / 4.0)  # Normalize to 0-1
            
            return AnomalyDetectionResult(
                anomaly_type='spike',
                severity=severity,
                confidence=confidence,
                affected_metrics=[metric_name],
                detected_at=datetime.now(),
                expected_range=(baseline['mean'] - 2*baseline['std'], baseline['mean'] + 2*baseline['std']),
                actual_values=[current_value],
                recommendations=self._get_spike_recommendations(metric_name, z_score),
                auto_action_triggered=False
            )

        return None


# 3. Early-Warning Micro-Signal Detection (Pre-failure)

@dataclass
class MicroSignal:
    """Early-warning micro-signal for pre-failure detection."""
    signal_type: str  # 'velocity_decay', 'engagement_half_life', 'retention_collapse', 'comment_divergence'
    severity: str  # 'low', 'medium', 'high', 'critical'
    confidence: float  # 0-1 confidence in signal
    detected_at: datetime
    video_id: str
    signal_value: float  # Actual measured value
    threshold_value: float  # Threshold that was crossed
    time_window: int  # Hours/days of analysis
    trend_slope: float  # Slope of the trend (negative = decay)
    recommendations: List[str]  # Recommended preemptive actions
    auto_action_triggered: bool  # Whether automatic action was taken


@dataclass
class EarlyWarningConfig:
    """Configuration for early-warning thresholds."""
    velocity_decay_threshold: float = -0.15  # 15% decay per hour
    velocity_decay_window: int = 3  # 3 hours of decay
    engagement_half_life_threshold: float = 2.0  # Hours
    retention_collapse_threshold: float = 0.3  # 30% retention in first 3 hours
    comment_divergence_threshold: float = 0.2  # 20% divergence from like ratio
    pre_boost_confidence: float = 0.7  # Confidence required for auto pre-boost
    intervention_lead_time: int = 6  # Hours before baseline failure


class EarlyWarningMicroSignalDetector:
    """Early-warning micro-signal detection for pre-failure intervention."""
    
    def __init__(self, config: EarlyWarningConfig = None):
        self.config = config or EarlyWarningConfig()
        self.logger = logging.getLogger('EarlyWarning')
        
        # Signal detection thresholds
        self.thresholds = {
            'velocity_decay': {
                'critical': -0.25,  # 25% decay per hour
                'high': -0.20,      # 20% decay per hour
                'medium': -0.15,    # 15% decay per hour
                'low': -0.10        # 10% decay per hour
            },
            'engagement_half_life': {
                'critical': 1.0,    # 1 hour half-life
                'high': 1.5,        # 1.5 hour half-life
                'medium': 2.0,      # 2 hour half-life
                'low': 3.0          # 3 hour half-life
            },
            'retention_collapse': {
                'critical': 0.2,    # 20% retention
                'high': 0.25,       # 25% retention
                'medium': 0.3,      # 30% retention
                'low': 0.35         # 35% retention
            },
            'comment_divergence': {
                'critical': 0.3,    # 30% divergence
                'high': 0.25,       # 25% divergence
                'medium': 0.2,      # 20% divergence
                'low': 0.15         # 15% divergence
            }
        }
        
        # Historical signal tracking
        self.signal_history: List[MicroSignal] = []
        self.video_signals: Dict[str, List[MicroSignal]] = defaultdict(list)
        
    def detect_micro_signals(self, video_data: pd.DataFrame, video_id: str) -> List[MicroSignal]:
        """
        Detect early-warning micro-signals for a specific video.
        
        Args:
            video_data: Historical data for the video
            video_id: Video identifier
            
        Returns:
            List[MicroSignal]: Detected micro-signals with recommendations
        """
        signals = []
        
        if video_data.empty or len(video_data) < 3:
            return signals
        
        # Sort by timestamp for time series analysis
        video_data = video_data.sort_values('timestamp')
        
        # 1. Velocity Decay Detection
        velocity_signal = self._detect_velocity_decay(video_data, video_id)
        if velocity_signal:
            signals.append(velocity_signal)
        
        # 2. Engagement Half-Life Detection
        engagement_signal = self._detect_engagement_half_life(video_data, video_id)
        if engagement_signal:
            signals.append(engagement_signal)
        
        # 3. Retention Collapse Detection
        retention_signal = self._detect_retention_collapse(video_data, video_id)
        if retention_signal:
            signals.append(retention_signal)
        
        # 4. Comment-to-Like Divergence Detection
        divergence_signal = self._detect_comment_divergence(video_data, video_id)
        if divergence_signal:
            signals.append(divergence_signal)
        
        # Auto-trigger pre-boost for critical signals
        for signal in signals:
            if signal.severity == 'critical' and signal.confidence >= self.config.pre_boost_confidence:
                self._trigger_pre_boost(signal)
                signal.auto_action_triggered = True
        
        # Store signals for tracking
        for signal in signals:
            self.signal_history.append(signal)
            self.video_signals[video_id].append(signal)
        
        return signals
    
    def _detect_velocity_decay(self, video_data: pd.DataFrame, video_id: str) -> Optional[MicroSignal]:
        """Detect velocity decay slope over recent hours."""
        try:
            # Calculate velocity (views per hour) for each timestamp
            video_data = video_data.copy()
            video_data['hours_since_publish'] = (
                pd.to_datetime(video_data['timestamp']) - pd.to_datetime(video_data['publish_date'])
            ).dt.total_seconds() / 3600
            
            # Filter to recent data (last 6 hours)
            current_time = video_data['hours_since_publish'].max()
            recent_data = video_data[
                (video_data['hours_since_publish'] >= current_time - 6) &
                (video_data['hours_since_publish'] > 0)
            ]
            
            if len(recent_data) < 3:
                return None
            
            # Calculate velocity and fit linear trend
            recent_data = recent_data.sort_values('hours_since_publish')
            x = recent_data['hours_since_publish'].values
            y = recent_data['views'].values
            
            # Linear regression to find slope
            slope = np.polyfit(x, y, 1)[0]  # views per hour slope
            
            # Normalize slope as percentage decay
            avg_velocity = np.mean(y)
            if avg_velocity > 0:
                decay_rate = slope / avg_velocity  # Normalized decay rate
            else:
                decay_rate = 0
            
            # Check if decay exceeds threshold
            if decay_rate < self.thresholds['velocity_decay']['low']:
                severity = self._calculate_signal_severity(decay_rate, 'velocity_decay')
                confidence = min(1.0, abs(decay_rate) / 0.3)  # Normalize confidence
                
                return MicroSignal(
                    signal_type='velocity_decay',
                    severity=severity,
                    confidence=confidence,
                    detected_at=datetime.now(),
                    video_id=video_id,
                    signal_value=decay_rate,
                    threshold_value=self.config.velocity_decay_threshold,
                    time_window=6,
                    trend_slope=decay_rate,
                    recommendations=self._get_velocity_decay_recommendations(decay_rate, severity),
                    auto_action_triggered=False
                )
        
        except Exception as e:
            self.logger.warning(f"Error detecting velocity decay for {video_id}: {e}")
        
        return None
    
    def _trigger_pre_boost(self, signal: MicroSignal):
        """Trigger automatic pre-boost for critical signals."""
        self.logger.critical(f" PRE-BOOST TRIGGERED: {signal.signal_type} for {signal.video_id}")
        self.logger.critical(f" Signal Value: {signal.signal_value:.3f}, Severity: {signal.severity}")
        self.logger.critical(f" Confidence: {signal.confidence:.2f}, Recommendations: {len(signal.recommendations)}")
        
        # In production, this would trigger actual pre-boost
        self.logger.info(f"Pre-boost recommendations: {signal.recommendations}")
    
    def _calculate_signal_severity(self, value: float, signal_type: str, reverse: bool = False) -> str:
        """Calculate severity level based on signal value."""
        thresholds = self.thresholds[signal_type]
        
        if reverse:  # Lower values are more severe (like half-life)
            if value <= thresholds['critical']:
                return 'critical'
            elif value <= thresholds['high']:
                return 'high'
            elif value <= thresholds['medium']:
                return 'medium'
            else:
                return 'low'
        else:  # Higher values are more severe (like decay rate)
            if value >= thresholds['critical']:
                return 'critical'
            elif value >= thresholds['high']:
                return 'high'
            elif value >= thresholds['medium']:
                return 'medium'
            else:
                return 'low'
    
    def _get_velocity_decay_recommendations(self, decay_rate: float, severity: str) -> List[str]:
        """Get recommendations for velocity decay signals."""
        recommendations = [
            "Analyze content performance trends",
            "Check distribution channel effectiveness",
            "Review audience engagement patterns"
        ]
        
        if severity == 'critical':
            recommendations.extend([
                " CRITICAL: Immediate pre-boost recommended",
                "Investigate potential content fatigue",
                "Consider content refresh or repositioning"
            ])
        elif severity == 'high':
            recommendations.extend([
                "Schedule pre-boost within 2 hours",
                "Optimize content metadata and tags"
            ])
        
        return recommendations
    
    def _detect_engagement_half_life(self, video_data: pd.DataFrame, video_id: str) -> Optional[MicroSignal]:
        """Detect engagement half-life collapse."""
        try:
            # Calculate engagement rate over time
            video_data = video_data.copy()
            video_data['engagement_rate'] = (
                (video_data['likes'] + video_data['comments'] * 2 + video_data['shares'] * 3) / 
                video_data['views'].replace(0, 1)
            )
            
            # Sort by timestamp
            video_data = video_data.sort_values('timestamp')
            
            # Find peak engagement
            peak_idx = video_data['engagement_rate'].idxmax()
            peak_engagement = video_data.loc[peak_idx, 'engagement_rate']
            peak_time = pd.to_datetime(video_data.loc[peak_idx, 'timestamp'])
            
            # Calculate half-life (time to drop to 50% of peak)
            half_life_target = peak_engagement * 0.5
            post_peak_data = video_data[video_data['timestamp'] > peak_time]
            
            if len(post_peak_data) < 2:
                return None
            
            # Find when engagement dropped to half-life
            for _, row in post_peak_data.iterrows():
                if row['engagement_rate'] <= half_life_target:
                    half_life_time = (pd.to_datetime(row['timestamp']) - peak_time).total_seconds() / 3600
                    break
            else:
                # Engagement hasn't dropped to half-life yet
                return None
            
            # Check if half-life is too short
            if half_life_time < self.thresholds['engagement_half_life']['low']:
                severity = self._calculate_signal_severity(half_life_time, 'engagement_half_life', reverse=True)
                confidence = min(1.0, (3.0 - half_life_time) / 2.0)  # Shorter half-life = higher confidence
                
                return MicroSignal(
                    signal_type='engagement_half_life',
                    severity=severity,
                    confidence=confidence,
                    detected_at=datetime.now(),
                    video_id=video_id,
                    signal_value=half_life_time,
                    threshold_value=self.config.engagement_half_life_threshold,
                    time_window=int(half_life_time),
                    trend_slope=-half_life_time,  # Negative slope indicates decay
                    recommendations=self._get_engagement_half_life_recommendations(half_life_time, severity),
                    auto_action_triggered=False
                )
        
        except Exception as e:
            self.logger.warning(f"Error detecting engagement half-life for {video_id}: {e}")
        
        return None
    
    def _get_engagement_half_life_recommendations(self, half_life: float, severity: str) -> List[str]:
        """Get recommendations for engagement half-life signals."""
        recommendations = [
            "Review content hook and opening",
            "Analyze audience retention patterns",
            "Check content pacing and structure"
        ]
        
        if severity == 'critical':
            recommendations.extend([
                " CRITICAL: Content may need immediate re-edit",
                "Investigate opening 30 seconds performance",
                "Consider A/B testing different hooks"
            ])
        elif severity == 'high':
            recommendations.extend([
                "Optimize content for better retention",
                "Review competitor content patterns"
            ])
        
        return recommendations
    
    def _detect_retention_collapse(self, video_data: pd.DataFrame, video_id: str) -> Optional[MicroSignal]:
        """Detect retention collapse in first 1-3 hours."""
        try:
            # Filter to first 3 hours of data
            video_data = video_data.copy()
            video_data['hours_since_publish'] = (
                pd.to_datetime(video_data['timestamp']) - pd.to_datetime(video_data['publish_date'])
            ).dt.total_seconds() / 3600
            
            early_data = video_data[
                (video_data['hours_since_publish'] >= 0) &
                (video_data['hours_since_publish'] <= 3)
            ].sort_values('hours_since_publish')
            
            if len(early_data) < 2:
                return None
            
            # Check retention rate in early period
            avg_retention = early_data['retention_rate'].mean()
            
            # Check if retention collapsed
            if avg_retention < self.thresholds['retention_collapse']['low']:
                severity = self._calculate_signal_severity(avg_retention, 'retention_collapse')
                confidence = min(1.0, (0.5 - avg_retention) / 0.3)  # Lower retention = higher confidence
                
                return MicroSignal(
                    signal_type='retention_collapse',
                    severity=severity,
                    confidence=confidence,
                    detected_at=datetime.now(),
                    video_id=video_id,
                    signal_value=avg_retention,
                    threshold_value=self.config.retention_collapse_threshold,
                    time_window=3,
                    trend_slope=avg_retention - 0.5,  # Deviation from expected 50%
                    recommendations=self._get_retention_collapse_recommendations(avg_retention, severity),
                    auto_action_triggered=False
                )
        
        except Exception as e:
            self.logger.warning(f"Error detecting retention collapse for {video_id}: {e}")
        
        return None
    
    def _get_retention_collapse_recommendations(self, retention: float, severity: str) -> List[str]:
        """Get recommendations for retention collapse signals."""
        recommendations = [
            "Investigate content quality issues",
            "Check technical playback problems",
            "Review audience targeting accuracy"
        ]
        
        if severity == 'critical':
            recommendations.extend([
                " CRITICAL: Content may have fundamental issues",
                "Pause distribution until investigation complete",
                "Consider content replacement"
            ])
        elif severity == 'high':
            recommendations.extend([
                "Review content production pipeline",
                "Check for platform-specific issues"
            ])
        
        return recommendations
    
    def _detect_comment_divergence(self, video_data: pd.DataFrame, video_id: str) -> Optional[MicroSignal]:
        """Detect comment-to-like ratio divergence."""
        try:
            # Calculate comment-to-like ratio over time
            video_data = video_data.copy()
            video_data['comment_like_ratio'] = video_data['comments'] / video_data['likes'].replace(0, 1)
            
            # Get recent data
            recent_data = video_data.tail(10)  # Last 10 data points
            
            if len(recent_data) < 5:
                return None
            
            # Calculate average recent ratio
            avg_ratio = recent_data['comment_like_ratio'].mean()
            
            # Expected ratio based on platform and content type
            expected_ratio = 0.1  # 10% of likes should be comments
            
            # Calculate divergence
            divergence = abs(avg_ratio - expected_ratio) / expected_ratio
            
            # Check if divergence exceeds threshold
            if divergence > self.thresholds['comment_divergence']['low']:
                severity = self._calculate_signal_severity(divergence, 'comment_divergence')
                confidence = min(1.0, divergence / 0.5)  # Higher divergence = higher confidence
                
                return MicroSignal(
                    signal_type='comment_divergence',
                    severity=severity,
                    confidence=confidence,
                    detected_at=datetime.now(),
                    video_id=video_id,
                    signal_value=divergence,
                    threshold_value=self.config.comment_divergence_threshold,
                    time_window=10,
                    trend_slope=avg_ratio - expected_ratio,
                    recommendations=self._get_comment_divergence_recommendations(avg_ratio, severity),
                    auto_action_triggered=False
                )
        
        except Exception as e:
            self.logger.warning(f"Error detecting comment divergence for {video_id}: {e}")
        
        return None
    
    def _get_comment_divergence_recommendations(self, ratio: float, severity: str) -> List[str]:
        """Get recommendations for comment divergence signals."""
        recommendations = [
            "Analyze audience engagement patterns",
            "Review content call-to-action effectiveness",
            "Check comment moderation policies"
        ]
        
        if ratio < 0.05:  # Low engagement
            recommendations.extend([
                "Improve content engagement prompts",
                "Add discussion-provoking elements"
            ])
        elif ratio > 0.2:  # High engagement
            recommendations.extend([
                "Monitor for spam or bot activity",
                "Review comment quality and relevance"
            ])
        
        if severity == 'critical':
            recommendations.append(" CRITICAL: Investigate unusual engagement patterns")
        
        return recommendations
    
    def get_video_signal_summary(self, video_id: str) -> Dict:
        """Get summary of all signals for a specific video."""
        video_signals = self.video_signals.get(video_id, [])
        
        if not video_signals:
            return {
                'video_id': video_id,
                'total_signals': 0,
                'critical_signals': 0,
                'auto_actions_triggered': 0,
                'signal_types': [],
                'latest_signal': None
            }
        
        return {
            'video_id': video_id,
            'total_signals': len(video_signals),
            'critical_signals': len([s for s in video_signals if s.severity == 'critical']),
            'auto_actions_triggered': len([s for s in video_signals if s.auto_action_triggered]),
            'signal_types': list(set(s.signal_type for s in video_signals)),
            'latest_signal': video_signals[-1] if video_signals else None,
            'signal_timeline': [
                {
                    'timestamp': s.detected_at.isoformat(),
                    'signal_type': s.signal_type,
                    'severity': s.severity,
                    'confidence': s.confidence
                }
                for s in video_signals
            ]
        }
    
    def get_factory_signal_report(self) -> Dict:
        """Get comprehensive factory-wide signal report."""
        if not self.signal_history:
            return {
                'total_signals': 0,
                'critical_signals': 0,
                'auto_actions_triggered': 0,
                'signal_types': {},
                'severity_distribution': {},
                'recent_signals': [],
                'videos_with_signals': 0,
                'avg_confidence': 0.0
            }
        
        # Analyze all signals
        signal_types = defaultdict(int)
        severity_distribution = defaultdict(int)
        
        for signal in self.signal_history:
            signal_types[signal.signal_type] += 1
            severity_distribution[signal.severity] += 1
        
        # Recent signals (last 24 hours)
        recent_cutoff = datetime.now() - timedelta(hours=24)
        recent_signals = [
            s for s in self.signal_history 
            if s.detected_at >= recent_cutoff
        ]
        
        return {
            'total_signals': len(self.signal_history),
            'critical_signals': severity_distribution['critical'],
            'auto_actions_triggered': len([s for s in self.signal_history if s.auto_action_triggered]),
            'signal_types': dict(signal_types),
            'severity_distribution': dict(severity_distribution),
            'recent_signals': len(recent_signals),
            'videos_with_signals': len(self.video_signals),
            'avg_confidence': sum(s.confidence for s in self.signal_history) / len(self.signal_history) if self.signal_history else 0.0
        }


# 🔥 4. Confidence-Weighted Decision Logic System

@dataclass
class ConfidenceWeightedDecision:
    """Confidence-weighted decision with dynamic parameters."""
    base_action: str  # Base action type
    confidence: float  # 0-1 confidence score
    weighted_priority: float  # Confidence-adjusted priority
    weighted_severity: float  # Confidence-adjusted severity
    budget_allocation: float  # Confidence-based budget allocation
    retry_limit: int  # Confidence-based retry count
    cooldown_duration: timedelta  # Confidence-based cooldown
    action_intensity: str  # 'conservative', 'moderate', 'aggressive', 'maximum'
    expected_roi: float  # Expected return on investment
    risk_assessment: str  # 'low', 'medium', 'high', 'critical'
    decision_factors: Dict[str, float]  # Factors influencing decision
    timestamp: datetime
    expires_at: datetime


@dataclass
class ConfidenceThresholds:
    """Dynamic confidence thresholds for decision making."""
    ultra_high: float = 0.95  # Maximum confidence - maximum action
    high: float = 0.85       # High confidence - aggressive action
    medium: float = 0.70     # Medium confidence - moderate action
    low: float = 0.50        # Low confidence - conservative action
    ultra_low: float = 0.30  # Minimum confidence - minimal action
    critical: float = 0.90   # Critical threshold for emergency actions


@dataclass
class ActionIntensityProfile:
    """Profile for action intensity based on confidence."""
    intensity_level: str
    priority_multiplier: float
    severity_multiplier: float
    budget_multiplier: float
    cooldown_multiplier: float
    retry_multiplier: float
    roi_expectation: float
    risk_factor: float
    description: str
    

@dataclass
class ShadowEvaluationCohort:
    """Shadow evaluation cohort for A/B testing without actual intervention."""
    campaign_id: str
    video_ids: List[str]
    shadow_decisions: List[ConfidenceWeightedDecision]
    control_group_size: int
    evaluation_period: timedelta
    created_at: datetime
    status: str  # 'active', 'evaluated', 'completed'
    evaluated_at: Optional[datetime] = None
    uplift_analysis: Optional[Dict] = None
    effectiveness_scores: Optional[Dict] = None
    actual_performance: Optional[Dict] = None


# 🧠 Factory Learning Memory - Compounding Advantage System Dataclasses

@dataclass
class FactoryActionMemory:
    """Record of factory action outcomes for learning."""
    action_id: str
    action_type: str  # 'boost', 'optimize', 'repost', 'immediate_boost'
    niche: str
    video_id: str
    lifecycle_stage: str  # 'ignition', 'growth', 'maturity', 'decline'
    pre_action_metrics: Dict[str, float]
    post_action_metrics: Dict[str, float]
    action_parameters: Dict[str, Any]
    outcome_score: float  # -1.0 to 1.0
    effectiveness_rating: str  # 'highly_effective', 'effective', 'neutral', 'ineffective'
    confidence_level: float  # 0.0 to 1.0
    timestamp: datetime
    duration_days: int  # How long the effect lasted
    compounding_factor: float  # How much this influenced future actions


@dataclass
class NicheLearningProfile:
    """Accumulated learning profile for a specific niche."""
    niche: str
    total_actions: int
    successful_actions: int
    failed_actions: int
    most_effective_actions: Dict[str, List[float]]  # action_type -> [outcomes]
    lifecycle_preferences: Dict[str, Dict[str, List[float]]]  # stage -> action_type -> [outcomes]
    optimal_timing_windows: Dict[str, List[Tuple[int, float]]]  # action_type -> [(hour, outcome)]
    parameter_insights: Dict[str, Dict[str, List[Tuple[Any, float]]]]  # action_type -> param -> [(value, outcome)]
    last_updated: datetime
    learning_maturity: float  # 0.0 to 1.0 - how much we've learned


@dataclass
class LifecycleStageInsights:
    """Learning insights for specific lifecycle stages."""
    stage: str  # 'ignition', 'growth', 'maturity', 'decline'
    total_observations: int
    success_rates: Dict[str, List[float]]  # action_type -> [outcomes]
    optimal_parameters: Dict[str, Dict[str, List[Tuple[Any, float]]]]  # action_type -> param -> [(value, outcome)]
    timing_patterns: Dict[str, List[Tuple[int, float]]]  # action_type -> [(hour, outcome)]
    risk_factors: List[str]  # What makes actions fail at this stage
    success_factors: List[str]  # What makes actions succeed at this stage
    confidence_scores: Dict[str, List[float]]  # action_type -> [confidence]


@dataclass
class UpliftAnalysis:
    """Results from shadow evaluation uplift analysis."""
    true_uplift: float  # True performance uplift percentage
    statistical_significance: float  # P-value for significance testing
    confidence_interval: Tuple[float, float]  # 95% confidence interval
    effect_size: float  # Cohen's d effect size
    control_group_performance: Dict[str, float]
    treatment_group_performance: Dict[str, float]
    roi_difference: float  # ROI difference between groups
    budget_efficiency: float  # Budget efficiency ratio
    recommendation_strength: str  # 'strong', 'moderate', 'weak'


@dataclass
class BoosterEffectivenessScore:
    """Comprehensive booster effectiveness scoring."""
    overall_effectiveness: float  # 0-100 overall score
    roi_effectiveness: float  # ROI-based effectiveness
    budget_efficiency: float  # Budget utilization efficiency
    prediction_accuracy: float  # ML prediction accuracy
    risk_adjusted_return: float  # Risk-adjusted return score
    consistency_score: float  # Consistency across similar actions
    scalability_factor: float  # Scalability potential score
    recommendation: str  # 'deploy', 'test_further', 'reject'


class ConfidenceWeightedDecisionEngine:
    """
    Advanced confidence-weighted decision logic for factory operations.
    
    This engine dynamically adjusts all factory decisions based on ML confidence,
    optimizing budget allocation, action intensity, and resource utilization.
    """
    
    def __init__(self, thresholds: ConfidenceThresholds = None):
        self.thresholds = thresholds or ConfidenceThresholds()
        self.logger = logging.getLogger('ConfidenceEngine')
        
        # Action intensity profiles
        self.intensity_profiles = {
            'maximum': ActionIntensityProfile(
                intensity_level='maximum',
                priority_multiplier=1.5,
                severity_multiplier=1.4,
                budget_multiplier=1.3,
                cooldown_multiplier=0.5,
                retry_multiplier=2.0,
                roi_expectation=1.8,
                risk_factor=0.9,
                description='Maximum intensity action for ultra-high confidence'
            ),
            'aggressive': ActionIntensityProfile(
                intensity_level='aggressive',
                priority_multiplier=1.3,
                severity_multiplier=1.2,
                budget_multiplier=1.2,
                cooldown_multiplier=0.7,
                retry_multiplier=1.5,
                roi_expectation=1.5,
                risk_factor=0.7,
                description='Aggressive action for high confidence'
            ),
            'moderate': ActionIntensityProfile(
                intensity_level='moderate',
                priority_multiplier=1.0,
                severity_multiplier=1.0,
                budget_multiplier=1.0,
                cooldown_multiplier=1.0,
                retry_multiplier=1.0,
                roi_expectation=1.2,
                risk_factor=0.5,
                description='Moderate action for medium confidence'
            ),
            'conservative': ActionIntensityProfile(
                intensity_level='conservative',
                priority_multiplier=0.8,
                severity_multiplier=0.7,
                budget_multiplier=0.6,
                cooldown_multiplier=1.5,
                retry_multiplier=0.5,
                roi_expectation=0.8,
                risk_factor=0.3,
                description='Conservative action for low confidence'
            ),
            'minimal': ActionIntensityProfile(
                intensity_level='minimal',
                priority_multiplier=0.5,
                severity_multiplier=0.4,
                budget_multiplier=0.3,
                cooldown_multiplier=2.0,
                retry_multiplier=0.25,
                roi_expectation=0.5,
                risk_factor=0.1,
                description='Minimal action for ultra-low confidence'
            )
        }
        
        # Decision history for learning
        self.decision_history: List[ConfidenceWeightedDecision] = []
        self.shadow_decision_history: List[ConfidenceWeightedDecision] = []
        self.shadow_cohorts: Dict[str, ShadowEvaluationCohort] = {}
        self.performance_metrics: Dict[str, float] = {}
        
    def calculate_confidence_weighted_decision(self, 
                                            base_action: str,
                                            confidence: float,
                                            context: Dict = None) -> ConfidenceWeightedDecision:
        """
        Calculate confidence-weighted decision with dynamic parameters.
        
        Args:
            base_action: Base action type (boost, optimize, repost, etc.)
            confidence: ML confidence score (0-1)
            context: Additional context for decision making
            
        Returns:
            ConfidenceWeightedDecision: Weighted decision with all parameters
        """
        context = context or {}
        
        # Determine action intensity based on confidence
        intensity = self._determine_action_intensity(confidence, base_action, context)
        profile = self.intensity_profiles[intensity]
        
        # Calculate weighted parameters
        weighted_priority = self._calculate_weighted_priority(base_action, confidence, profile)
        weighted_severity = self._calculate_weighted_severity(base_action, confidence, profile)
        budget_allocation = self._calculate_budget_allocation(confidence, profile, context)
        retry_limit = self._calculate_retry_limit(confidence, profile, base_action)
        cooldown_duration = self._calculate_cooldown_duration(confidence, profile, base_action)
        
        # Calculate expected ROI and risk
        expected_roi = self._calculate_expected_roi(confidence, profile, base_action, context)
        risk_assessment = self._assess_risk(confidence, profile, base_action, context)
        
        # Generate decision factors
        decision_factors = self._generate_decision_factors(confidence, profile, context)
        
        # Create decision
        decision = ConfidenceWeightedDecision(
            base_action=base_action,
            confidence=confidence,
            weighted_priority=weighted_priority,
            weighted_severity=weighted_severity,
            budget_allocation=budget_allocation,
            retry_limit=retry_limit,
            cooldown_duration=cooldown_duration,
            action_intensity=intensity,
            expected_roi=expected_roi,
            risk_assessment=risk_assessment,
            decision_factors=decision_factors,
            timestamp=datetime.now(),
            expires_at=datetime.now() + timedelta(hours=24)  # Decision valid for 24 hours
        )
        
        # Store decision for learning
        self.decision_history.append(decision)
        
        self.logger.info(f"Confidence-weighted decision: {base_action} -> {intensity} (confidence: {confidence:.2f})")
        self.logger.info(f"  Priority: {weighted_priority:.2f}, Severity: {weighted_severity:.2f}, Budget: {budget_allocation:.2f}")
        self.logger.info(f"  Expected ROI: {expected_roi:.2f}, Risk: {risk_assessment}")
        
        return decision
    
    def calculate_shadow_decision(self, 
                               base_action: str,
                               confidence: float,
                               context: Dict = None,
                               shadow_mode: bool = True) -> ConfidenceWeightedDecision:
        """
        Calculate confidence-weighted decision with shadow evaluation support.
        
        Args:
            base_action: Base action type (boost, optimize, repost, etc.)
            confidence: ML confidence score (0-1)
            context: Additional context for decision making
            shadow_mode: If True, run in shadow mode (no actual action)
            
        Returns:
            ConfidenceWeightedDecision: Weighted decision with shadow tracking
        """
        context = context or {}
        
        # Determine action intensity based on confidence
        intensity = self._determine_action_intensity(confidence, base_action, context)
        profile = self.intensity_profiles[intensity]
        
        # Calculate weighted parameters
        weighted_priority = self._calculate_weighted_priority(base_action, confidence, profile)
        weighted_severity = self._calculate_weighted_severity(base_action, confidence, profile)
        budget_allocation = self._calculate_budget_allocation(confidence, profile, context)
        retry_limit = self._calculate_retry_limit(confidence, profile, base_action)
        cooldown_duration = self._calculate_cooldown_duration(confidence, profile, base_action)
        
        # Calculate expected ROI and risk
        expected_roi = self._calculate_expected_roi(confidence, profile, base_action, context)
        risk_assessment = self._assess_risk(confidence, profile, base_action, context)
        
        # Generate decision factors
        decision_factors = self._generate_decision_factors(confidence, profile, context)
        
        # Add shadow mode factors
        if shadow_mode:
            decision_factors['shadow_mode'] = True
            decision_factors['simulation_only'] = True
            decision_factors['actual_budget_spent'] = 0.0
            decision_factors['predicted_vs_actual'] = 'simulation_pending'
        
        # Create decision
        decision = ConfidenceWeightedDecision(
            base_action=base_action,
            confidence=confidence,
            weighted_priority=weighted_priority,
            weighted_severity=weighted_severity,
            budget_allocation=budget_allocation,
            retry_limit=retry_limit,
            cooldown_duration=cooldown_duration,
            action_intensity=intensity,
            expected_roi=expected_roi,
            risk_assessment=risk_assessment,
            decision_factors=decision_factors,
            timestamp=datetime.now(),
            expires_at=datetime.now() + timedelta(hours=24)  # Decision valid for 24 hours
        )
        
        # Store decision with shadow tracking
        if shadow_mode:
            self.shadow_decision_history.append(decision)
            self.logger.info(f"Shadow decision: {base_action} -> {intensity} (confidence: {confidence:.2f})")
        else:
            self.decision_history.append(decision)
            self.logger.info(f"Actual decision: {base_action} -> {intensity} (confidence: {confidence:.2f})")
        
        self.logger.info(f"  Priority: {weighted_priority:.2f}, Severity: {weighted_severity:.2f}, Budget: {budget_allocation:.2f}")
        self.logger.info(f"  Expected ROI: {expected_roi:.2f}, Risk: {risk_assessment}")
        
        return decision
    
    def run_shadow_evaluation_campaign(self, 
                                     video_ids: List[str],
                                     confidence_scores: List[float],
                                     actions: List[str],
                                     context: Dict = None,
                                     evaluation_period_days: int = 30) -> Dict:
        """
        Run a comprehensive shadow evaluation campaign.
        
        Args:
            video_ids: List of video IDs to evaluate
            confidence_scores: Corresponding ML confidence scores
            actions: Recommended actions for each video
            context: Additional context for decisions
            evaluation_period_days: Period to track results
            
        Returns:
            Dict: Shadow campaign results and uplift analysis
        """
        campaign_id = f"shadow_campaign_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        
        self.logger.info(f"Starting shadow evaluation campaign: {campaign_id}")
        self.logger.info(f"  Videos: {len(video_ids)}, Period: {evaluation_period_days} days")
        
        # Generate shadow decisions
        shadow_decisions = []
        for video_id, confidence, action in zip(video_ids, confidence_scores, actions):
            decision_context = context.copy() if context else {}
            decision_context['video_id'] = video_id
            decision_context['campaign_id'] = campaign_id
            
            shadow_decision = self.calculate_shadow_decision(
                action, confidence, decision_context, shadow_mode=True
            )
            shadow_decisions.append(shadow_decision)
        
        # Create shadow cohort
        shadow_cohort = ShadowEvaluationCohort(
            campaign_id=campaign_id,
            video_ids=video_ids,
            shadow_decisions=shadow_decisions,
            control_group_size=len(video_ids) // 2,  # 50% control group
            evaluation_period=timedelta(days=evaluation_period_days),
            created_at=datetime.now(),
            status='active'
        )
        
        self.shadow_cohorts[campaign_id] = shadow_cohort
        
        # Calculate campaign projections
        projections = self._calculate_shadow_campaign_projections(shadow_cohort)
        
        results = {
            'campaign_id': campaign_id,
            'status': 'shadow_active',
            'total_decisions': len(shadow_decisions),
            'evaluation_period_days': evaluation_period_days,
            'projections': projections,
            'shadow_cohort': shadow_cohort,
            'created_at': datetime.now().isoformat(),
            'next_evaluation_date': (datetime.now() + timedelta(days=evaluation_period_days)).isoformat()
        }
        
        self.logger.info(f"Shadow campaign {campaign_id} initialized with {len(shadow_decisions)} decisions")
        self.logger.info(f"  Projected ROI: {projections['projected_roi']:.2f}x")
        self.logger.info(f"  Projected budget impact: ${projections['projected_budget_impact']:.2f}")
        
        return results
    
    def evaluate_shadow_campaign_results(self, campaign_id: str) -> Dict:
        """
        Evaluate shadow campaign results vs actual performance.
        
        Args:
            campaign_id: Shadow campaign identifier
            
        Returns:
            Dict: Comprehensive uplift analysis and effectiveness metrics
        """
        if campaign_id not in self.shadow_cohorts:
            return {'error': f'Campaign {campaign_id} not found'}
        
        cohort = self.shadow_cohorts[campaign_id]
        
        # Get actual performance data for shadow cohort videos
        actual_performance = self._get_actual_performance_data(cohort.video_ids)
        
        # Calculate uplift metrics
        uplift_analysis = self._calculate_uplift_analysis(cohort, actual_performance)
        
        # Generate effectiveness scores
        effectiveness_scores = self._calculate_booster_effectiveness_scores(cohort, uplift_analysis)
        
        # RL reward accuracy assessment
        rl_reward_accuracy = self._calculate_rl_reward_accuracy(cohort, uplift_analysis)
        
        # Update cohort status
        cohort.status = 'evaluated'
        cohort.evaluated_at = datetime.now()
        cohort.uplift_analysis = uplift_analysis
        cohort.effectiveness_scores = effectiveness_scores
        
        results = {
            'campaign_id': campaign_id,
            'evaluation_status': 'completed',
            'uplift_analysis': uplift_analysis,
            'effectiveness_scores': effectiveness_scores,
            'rl_reward_accuracy': rl_reward_accuracy,
            'true_uplift_measurement': uplift_analysis['true_uplift'],
            'booster_effectiveness_score': effectiveness_scores['overall_effectiveness'],
            'recommendation': self._generate_campaign_recommendations(uplift_analysis, effectiveness_scores),
            'evaluated_at': datetime.now().isoformat()
        }
        
        self.logger.info(f"Shadow campaign {campaign_id} evaluation completed")
        self.logger.info(f"  True uplift: {uplift_analysis['true_uplift']:.2f}%")
        self.logger.info(f"  Booster effectiveness: {effectiveness_scores['overall_effectiveness']:.2f}")
        
        return results
    
    def get_shadow_mode_dashboard(self) -> Dict:
        """
        Get comprehensive shadow mode dashboard data.
        
        Returns:
            Dict: Complete shadow mode analytics and insights
        """
        active_campaigns = [c for c in self.shadow_cohorts.values() if c.status == 'active']
        evaluated_campaigns = [c for c in self.shadow_cohorts.values() if c.status == 'evaluated']
        
        # Calculate aggregate metrics
        total_shadow_decisions = len(self.shadow_decision_history)
        total_actual_decisions = len(self.decision_history)
        
        # Calculate average uplift from evaluated campaigns
        avg_uplift = 0.0
        if evaluated_campaigns:
            avg_uplift = sum(c.uplift_analysis.get('true_uplift', 0) for c in evaluated_campaigns) / len(evaluated_campaigns)
        
        # Calculate effectiveness distribution
        effectiveness_scores = [c.effectiveness_scores.get('overall_effectiveness', 0) for c in evaluated_campaigns]
        
        dashboard = {
            'shadow_mode_status': {
                'enabled': True,
                'total_shadow_decisions': total_shadow_decisions,
                'total_actual_decisions': total_actual_decisions,
                'shadow_to_actual_ratio': total_shadow_decisions / max(1, total_actual_decisions),
                'active_campaigns': len(active_campaigns),
                'evaluated_campaigns': len(evaluated_campaigns)
            },
            'performance_metrics': {
                'average_true_uplift': avg_uplift,
                'average_effectiveness_score': sum(effectiveness_scores) / max(1, len(effectiveness_scores)),
                'total_budget_saved_shadow': sum(d.decision_factors.get('predicted_budget_saved', 0) for d in self.shadow_decision_history),
                'prediction_accuracy': self._calculate_prediction_accuracy()
            },
            'active_campaigns': [
                {
                    'campaign_id': c.campaign_id,
                    'video_count': len(c.video_ids),
                    'days_remaining': max(0, (c.created_at + c.evaluation_period - datetime.now()).days),
                    'projected_roi': self._calculate_shadow_campaign_projections(c)['projected_roi']
                }
                for c in active_campaigns
            ],
            'recent_evaluations': [
                {
                    'campaign_id': c.campaign_id,
                    'evaluated_at': c.evaluated_at.isoformat() if c.evaluated_at else None,
                    'true_uplift': c.uplift_analysis.get('true_uplift', 0),
                    'effectiveness_score': c.effectiveness_scores.get('overall_effectiveness', 0),
                    'recommendation': self._generate_campaign_recommendations(c.uplift_analysis, c.effectiveness_scores)
                }
                for c in evaluated_campaigns[-5:]  # Last 5 evaluations
            ],
            'learning_insights': self._generate_shadow_mode_insights(),
            'generated_at': datetime.now().isoformat()
        }
        
        return dashboard
    
    def _determine_action_intensity(self, confidence: float, base_action: str, context: Dict) -> str:
        """Determine action intensity based on confidence and context."""
        
        # Adjust thresholds based on action type
        if base_action in ['immediate_boost', 'emergency_intervention']:
            # Critical actions require higher confidence for maximum intensity
            if confidence >= self.thresholds.ultra_high:
                return 'maximum'
            elif confidence >= self.thresholds.high:
                return 'aggressive'
            elif confidence >= self.thresholds.medium:
                return 'moderate'
            elif confidence >= self.thresholds.low:
                return 'conservative'
            else:
                return 'minimal'
        
        elif base_action in ['boost', 'optimize']:
            # Standard actions have normal thresholds
            if confidence >= self.thresholds.high:
                return 'aggressive'
            elif confidence >= self.thresholds.medium:
                return 'moderate'
            elif confidence >= self.thresholds.low:
                return 'conservative'
            else:
                return 'minimal'
        
        elif base_action in ['repost', 'monitor']:
            # Low-risk actions can be more aggressive with lower confidence
            if confidence >= self.thresholds.medium:
                return 'moderate'
            elif confidence >= self.thresholds.low:
                return 'conservative'
            else:
                return 'minimal'
        
        else:
            # Default behavior
            if confidence >= self.thresholds.high:
                return 'aggressive'
            elif confidence >= self.thresholds.medium:
                return 'moderate'
            elif confidence >= self.thresholds.low:
                return 'conservative'
            else:
                return 'minimal'
    
    def _calculate_weighted_priority(self, base_action: str, confidence: float, profile: ActionIntensityProfile) -> float:
        """Calculate confidence-weighted priority."""
        base_priority = self._get_base_priority(base_action)
        
        # Apply confidence weighting
        confidence_factor = 0.5 + (confidence * 0.5)  # 0.5 to 1.0 range
        weighted_priority = base_priority * profile.priority_multiplier * confidence_factor
        
        return max(1.0, min(10.0, weighted_priority))  # Clamp to 1-10 range
    
    def _calculate_weighted_severity(self, base_action: str, confidence: float, profile: ActionIntensityProfile) -> float:
        """Calculate confidence-weighted severity."""
        base_severity = self._get_base_severity(base_action)
        
        # Higher confidence increases severity for more aggressive action
        confidence_factor = 0.3 + (confidence * 0.7)  # 0.3 to 1.0 range
        weighted_severity = base_severity * profile.severity_multiplier * confidence_factor
        
        return max(1.0, min(100.0, weighted_severity))  # Clamp to 1-100 range
    
    def _calculate_budget_allocation(self, confidence: float, profile: ActionIntensityProfile, context: Dict) -> float:
        """Calculate confidence-weighted budget allocation."""
        base_budget = context.get('base_budget', 100.0)  # Default base budget
        
        # Adjust budget based on confidence and profile
        confidence_factor = 0.2 + (confidence * 0.8)  # 0.2 to 1.0 range
        weighted_budget = base_budget * profile.budget_multiplier * confidence_factor
        
        # Consider factory performance
        factory_performance = context.get('factory_performance', 1.0)
        weighted_budget *= factory_performance
        
        return max(10.0, weighted_budget)  # Minimum budget of 10
    
    def _calculate_retry_limit(self, confidence: float, profile: ActionIntensityProfile, base_action: str) -> int:
        """Calculate confidence-weighted retry limit."""
        base_retries = self._get_base_retry_limit(base_action)
        
        # Higher confidence allows more retries
        confidence_factor = 0.25 + (confidence * 0.75)  # 0.25 to 1.0 range
        weighted_retries = int(base_retries * profile.retry_multiplier * confidence_factor)
        
        return max(0, min(5, weighted_retries))  # Clamp to 0-5 range
    
    def _calculate_cooldown_duration(self, confidence: float, profile: ActionIntensityProfile, base_action: str) -> timedelta:
        """Calculate confidence-weighted cooldown duration."""
        base_cooldown = self._get_base_cooldown(base_action)
        
        # Higher confidence reduces cooldown
        confidence_factor = 1.0 - (confidence * 0.5)  # 1.0 to 0.5 range
        weighted_cooldown = base_cooldown * profile.cooldown_multiplier * confidence_factor
        
        return max(timedelta(minutes=15), weighted_cooldown)  # Minimum 15 minutes
    
    def _calculate_expected_roi(self, confidence: float, profile: ActionIntensityProfile, base_action: str, context: Dict) -> float:
        """Calculate expected return on investment."""
        base_roi = self._get_base_roi(base_action)
        
        # ROI increases with confidence and intensity
        confidence_factor = 0.5 + (confidence * 0.5)  # 0.5 to 1.0 range
        expected_roi = base_roi * profile.roi_expectation * confidence_factor
        
        # Adjust for market conditions
        market_factor = context.get('market_factor', 1.0)
        expected_roi *= market_factor
        
        return max(0.1, expected_roi)  # Minimum ROI of 0.1
    
    def _assess_risk(self, confidence: float, profile: ActionIntensityProfile, base_action: str, context: Dict) -> str:
        """Assess risk level based on confidence and context."""
        base_risk = profile.risk_factor
        
        # Lower confidence increases risk
        confidence_risk = 1.0 - confidence
        
        # Action-specific risk
        action_risk = self._get_action_risk(base_action)
        
        # Context-specific risk
        context_risk = context.get('additional_risk', 0.0)
        
        total_risk = base_risk + confidence_risk + action_risk + context_risk
        
        if total_risk >= 0.8:
            return 'critical'
        elif total_risk >= 0.6:
            return 'high'
        elif total_risk >= 0.4:
            return 'medium'
        else:
            return 'low'
    
    def _generate_decision_factors(self, confidence: float, profile: ActionIntensityProfile, context: Dict) -> Dict[str, float]:
        """Generate factors that influenced the decision."""
        return {
            'confidence_score': confidence,
            'intensity_multiplier': profile.priority_multiplier,
            'budget_multiplier': profile.budget_multiplier,
            'risk_factor': profile.risk_factor,
            'roi_expectation': profile.roi_expectation,
            'factory_performance': context.get('factory_performance', 1.0),
            'market_conditions': context.get('market_factor', 1.0),
            'historical_success_rate': context.get('success_rate', 0.5),
            'resource_availability': context.get('resource_availability', 1.0)
        }
    
    def _get_base_priority(self, base_action: str) -> float:
        """Get base priority for action type."""
        priorities = {
            'immediate_boost': 1.0,
            'emergency_intervention': 1.0,
            'boost': 3.0,
            'optimize': 5.0,
            'repost': 7.0,
            'monitor': 9.0
        }
        return priorities.get(base_action, 5.0)
    
    def _get_base_severity(self, base_action: str) -> float:
        """Get base severity for action type."""
        severities = {
            'immediate_boost': 90.0,
            'emergency_intervention': 95.0,
            'boost': 70.0,
            'optimize': 50.0,
            'repost': 30.0,
            'monitor': 10.0
        }
        return severities.get(base_action, 50.0)
    
    def _get_base_retry_limit(self, base_action: str) -> int:
        """Get base retry limit for action type."""
        retries = {
            'immediate_boost': 3,
            'emergency_intervention': 2,
            'boost': 2,
            'optimize': 1,
            'repost': 1,
            'monitor': 0
        }
        return retries.get(base_action, 1)
    
    def _get_base_cooldown(self, base_action: str) -> timedelta:
        """Get base cooldown for action type."""
        cooldowns = {
            'immediate_boost': timedelta(hours=24),
            'emergency_intervention': timedelta(hours=12),
            'boost': timedelta(days=3),
            'optimize': timedelta(days=7),
            'repost': timedelta(days=14),
            'monitor': timedelta(days=30)
        }
        return cooldowns.get(base_action, timedelta(days=7))
    
    def _get_base_roi(self, base_action: str) -> float:
        """Get base ROI for action type."""
        rois = {
            'immediate_boost': 1.8,
            'emergency_intervention': 2.0,
            'boost': 1.5,
            'optimize': 1.2,
            'repost': 0.8,
            'monitor': 0.3
        }
        return rois.get(base_action, 1.0)
    
    def _get_action_risk(self, base_action: str) -> float:
        """Get risk factor for action type."""
        risks = {
            'immediate_boost': 0.3,
            'emergency_intervention': 0.4,
            'boost': 0.2,
            'optimize': 0.1,
            'repost': 0.05,
            'monitor': 0.01
        }
        return risks.get(base_action, 0.1)
    
    def _calculate_shadow_campaign_projections(self, cohort: ShadowEvaluationCohort) -> Dict:
        """Calculate projections for shadow campaign."""
        total_budget = sum(d.budget_allocation for d in cohort.shadow_decisions)
        expected_roi = sum(d.expected_roi for d in cohort.shadow_decisions) / len(cohort.shadow_decisions)
        projected_impact = sum(d.decision_factors.get('expected_impact', 0) for d in cohort.shadow_decisions)
        
        return {
            'projected_roi': expected_roi,
            'projected_budget_impact': total_budget,
            'projected_impact_score': projected_impact,
            'confidence_level': sum(d.confidence for d in cohort.shadow_decisions) / len(cohort.shadow_decisions),
            'risk_adjusted_projection': projected_impact * (1 - sum(d.decision_factors.get('risk_factor', 0) for d in cohort.shadow_decisions) / len(cohort.shadow_decisions))
        }
    
    def _get_actual_performance_data(self, video_ids: List[str]) -> Dict:
        """Get actual performance data for videos."""
        # This would integrate with factory metrics to get real performance
        # For now, return mock data
        return {
            'views': {video_id: 1000000 for video_id in video_ids},
            'engagement_rate': {video_id: 0.05 for video_id in video_ids},
            'roi': {video_id: 1.2 for video_id in video_ids}
        }
    
    def _calculate_uplift_analysis(self, cohort: ShadowEvaluationCohort, actual_performance: Dict) -> Dict:
        """Calculate true uplift analysis."""
        # Mock implementation - would use statistical analysis
        control_performance = 1.0  # Baseline
        treatment_performance = 1.25  # 25% uplift
        
        true_uplift = ((treatment_performance - control_performance) / control_performance) * 100
        
        return {
            'true_uplift': true_uplift,
            'statistical_significance': 0.05,  # P-value
            'confidence_interval': (true_uplift - 5, true_uplift + 5),
            'effect_size': 0.8,  # Cohen's d
            'control_group_performance': {'roi': control_performance},
            'treatment_group_performance': {'roi': treatment_performance},
            'roi_difference': treatment_performance - control_performance,
            'budget_efficiency': true_uplift / 100,
            'recommendation_strength': 'strong' if true_uplift > 20 else 'moderate'
        }
    
    def _calculate_booster_effectiveness_scores(self, cohort: ShadowEvaluationCohort, uplift_analysis: Dict) -> Dict:
        """Calculate booster effectiveness scores."""
        true_uplift = uplift_analysis['true_uplift']
        
        overall_effectiveness = min(100, max(0, true_uplift * 2))  # Scale to 0-100
        roi_effectiveness = min(100, max(0, uplift_analysis['roi_difference'] * 50))
        budget_efficiency = min(100, max(0, uplift_analysis['budget_efficiency'] * 100))
        prediction_accuracy = 85.0  # Mock - would calculate from actual vs predicted
        risk_adjusted_return = overall_effectiveness * (1 - 0.1)  # Risk adjustment
        consistency_score = 80.0  # Mock - would calculate from variance
        scalability_factor = 75.0  # Mock - would calculate from cohort size
        
        recommendation = 'deploy' if overall_effectiveness > 70 else 'test_further' if overall_effectiveness > 50 else 'reject'
        
        return {
            'overall_effectiveness': overall_effectiveness,
            'roi_effectiveness': roi_effectiveness,
            'budget_efficiency': budget_efficiency,
            'prediction_accuracy': prediction_accuracy,
            'risk_adjusted_return': risk_adjusted_return,
            'consistency_score': consistency_score,
            'scalability_factor': scalability_factor,
            'recommendation': recommendation
        }
    
    def _calculate_rl_reward_accuracy(self, cohort: ShadowEvaluationCohort, uplift_analysis: Dict) -> Dict:
        """Calculate RL reward accuracy."""
        predicted_rewards = [d.expected_roi for d in cohort.shadow_decisions]
        actual_rewards = [1.0 + (uplift_analysis['true_uplift'] / 100)] * len(cohort.shadow_decisions)
        
        # Calculate correlation between predicted and actual rewards
        correlation = 0.75  # Mock - would calculate actual correlation
        
        return {
            'prediction_correlation': correlation,
            'reward_accuracy': correlation * 100,
            'calibration_error': abs(1.0 - correlation),
            'reward_bias': 'positive' if correlation > 0.7 else 'neutral',
            'model_reliability': 'high' if correlation > 0.8 else 'medium' if correlation > 0.6 else 'low'
        }
    
    def _generate_campaign_recommendations(self, uplift_analysis: Dict, effectiveness_scores: Dict) -> str:
        """Generate campaign recommendations."""
        true_uplift = uplift_analysis['true_uplift']
        effectiveness = effectiveness_scores['overall_effectiveness']
        
        if true_uplift > 25 and effectiveness > 80:
            return "DEPLOY: Strong uplift with high effectiveness - proceed to full deployment"
        elif true_uplift > 15 and effectiveness > 60:
            return "TEST_FURTHER: Moderate uplift - expand to larger test group"
        elif true_uplift > 5 and effectiveness > 40:
            return "OPTIMIZE: Limited uplift - refine targeting and parameters"
        else:
            return "REJECT: Insufficient uplift - reconsider approach"
    
    def _calculate_prediction_accuracy(self) -> float:
        """Calculate overall prediction accuracy."""
        if not self.shadow_decision_history:
            return 0.0
        
        # Mock implementation - would compare predicted vs actual outcomes
        return 85.0
    
    def _generate_shadow_mode_insights(self) -> List[str]:
        """Generate insights from shadow mode data."""
        insights = []
        
        if len(self.shadow_decision_history) > 0:
            avg_confidence = sum(d.confidence for d in self.shadow_decision_history) / len(self.shadow_decision_history)
            insights.append(f"Average confidence in shadow decisions: {avg_confidence:.2f}")
            
            budget_saved = sum(d.budget_allocation for d in self.shadow_decision_history)
            insights.append(f"Budget saved through shadow mode: ${budget_saved:.2f}")
        
        if len(self.shadow_cohorts) > 0:
            insights.append(f"Active shadow campaigns: {len([c for c in self.shadow_cohorts.values() if c.status == 'active'])}")
        
        return insights
    
    def get_decision_performance_report(self) -> Dict:
        """Get performance report of confidence-weighted decisions."""
        if not self.decision_history:
            return {
                'total_decisions': 0,
                'avg_confidence': 0.0,
                'avg_roi': 0.0,
                'success_rate': 0.0,
                'intensity_distribution': {},
                'action_distribution': {}
            }
        
        # Calculate metrics
        total_decisions = len(self.decision_history)
        avg_confidence = sum(d.confidence for d in self.decision_history) / total_decisions
        avg_roi = sum(d.expected_roi for d in self.decision_history) / total_decisions
        
        # Distribution analysis
        intensity_dist = {}
        action_dist = {}
        
        for decision in self.decision_history:
            intensity_dist[decision.action_intensity] = intensity_dist.get(decision.action_intensity, 0) + 1
            action_dist[decision.base_action] = action_dist.get(decision.base_action, 0) + 1
        
        return {
            'total_decisions': total_decisions,
            'avg_confidence': avg_confidence,
            'avg_roi': avg_roi,
            'success_rate': self.performance_metrics.get('success_rate', 0.0),
            'intensity_distribution': intensity_dist,
            'action_distribution': action_dist,
            'recent_decisions': len([d for d in self.decision_history if (datetime.now() - d.timestamp).days <= 7])
        }


@dataclass
class MLPrediction:
    """Container for ML prediction results with confidence intervals."""
    predicted_views: int
    confidence_low: int
    confidence_high: int
    probability_baseline: float
    model_version: str
    prediction_timestamp: datetime


@dataclass
class VideoKPIs:
    """Container for individual video KPIs."""
    video_id: str
    timestamp: datetime
    total_views: int
    likes: int
    comments: int
    shares: int
    watch_time_hours: float
    avg_retention_rate: float
    engagement_rate: float
    growth_velocity: float
    virality_score: float
    days_since_publish: int
    meets_baseline: bool
    needs_intervention: bool
    projected_final_views: int


@dataclass
class BatchMetricsResult:
    """Container for batch processing results with performance metrics."""
    video_kpis: List[VideoKPIs]
    processing_time_ms: float
    videos_processed: int
    throughput_per_second: float
    batch_size: int
    ml_prediction_time_ms: float
    long_tail_time_ms: float


@dataclass
class RLStateVector:
    """8-dimensional normalized state vector for RL policy input."""
    virality_score_normalized: float  # 0-1 normalized virality
    baseline_gap_ratio: float        # 0-1 gap to baseline
    growth_velocity_normalized: float # 0-1 normalized velocity  
    engagement_rate_normalized: float # 0-1 engagement rate
    retention_rate_normalized: float  # 0-1 retention rate
    days_live_normalized: float       # 0-1 days since publish
    long_tail_score_normalized: float # 0-1 long-tail potential
    confidence_score_normalized: float # 0-1 ML prediction confidence


@dataclass
class RLFeedbackSignal:
    """Policy feedback signals for RL learning."""
    action_effectiveness: float       # -1 to 1, how effective the action was
    cost_benefit_ratio: float         # cost vs benefit of intervention
    urgency_score: float              # 0-1, how urgently action needed
    intervention_type: str            # type of intervention applied
    expected_improvement: float       # expected view improvement


@dataclass
class RLActionState:
    """Action-state mapping for RL policy learning."""
    video_id: str
    state_vector: RLStateVector
    action_taken: str                 # action applied
    reward: float                     # computed reward
    feedback_signal: RLFeedbackSignal
    next_state_vector: RLStateVector  # resulting state after action
    timestamp: datetime


@dataclass
class RLRewardSignal:
    """Complete reward signal for RL reward shaping."""
    video_id: str
    reward: float                     # -1 to +1, primary reward signal
    reward_components: Dict[str, float] # breakdown of reward components
    state_vector: RLStateVector       # current state
    action_state: Optional[RLActionState] # action-state mapping if available
    feedback_signal: Optional[RLFeedbackSignal] # policy feedback
    confidence_interval: Tuple[float, float] # reward uncertainty
    model_version: str                # ML model version used
    timestamp: datetime


class AbstractMLPredictor(ABC):
    """
    Abstract interface for ML engagement prediction models.
    
    Provides:
    - Batch prediction capabilities
    - Confidence intervals
    - Model versioning
    - Async support interface
    """
    
    @abstractmethod
    def predict_single(self, video_id: str, features: Dict) -> MLPrediction:
        """
        Predict final view count for a single video.
        
        Args:
            video_id: Unique video identifier
            features: Video features (views, engagement, retention, etc.)
        
        Returns:
            MLPrediction: Prediction with confidence intervals
        """
        pass
    
    @abstractmethod
    def predict_batch(self, video_features: List[Tuple[str, Dict]]) -> List[MLPrediction]:
        """
        Predict final view counts for multiple videos efficiently.
        
        Args:
            video_features: List of (video_id, features) tuples
        
        Returns:
            List[MLPrediction]: Batch predictions
        """
        pass
    
    @abstractmethod
    async def predict_async(self, video_id: str, features: Dict) -> MLPrediction:
        """
        Async prediction for non-blocking operations.
        
        Args:
            video_id: Unique video identifier
            features: Video features
        
        Returns:
            MLPrediction: Prediction with confidence intervals
        """
        pass
    
    @abstractmethod
    def get_model_info(self) -> Dict:
        """
        Get model metadata and version information.
        
        Returns:
            Dict: Model info including version, training date, accuracy metrics
        """
        pass


class MockMLPredictor(AbstractMLPredictor):
    """
    Mock ML predictor that simulates predictions with confidence intervals.
    Used when real ML models are not available.
    """
    
    def __init__(self, model_version: str = "mock_v1.0"):
        self.model_version = model_version
        self.prediction_count = 0
    
    def predict_single(self, video_id: str, features: Dict) -> MLPrediction:
        """Generate mock prediction with confidence intervals."""
        current_views = features.get('views', 0)
        engagement_rate = features.get('engagement_rate', 0.05)
        growth_velocity = features.get('growth_velocity', 1000)
        days_live = features.get('days_live', 1)
        
        # Simulate ML prediction with intelligent extrapolation
        base_projection = current_views + (growth_velocity * max(0, 90 - days_live) * 0.7)
        
        # Add engagement multiplier
        engagement_boost = 1.0 + (engagement_rate / 0.05) * 0.3
        predicted_views = int(base_projection * engagement_boost)
        
        # Generate confidence intervals (±20% for mock)
        confidence_range = int(predicted_views * 0.2)
        confidence_low = max(0, predicted_views - confidence_range)
        confidence_high = predicted_views + confidence_range
        
        # Calculate baseline achievement probability
        baseline_prob = min(1.0, predicted_views / 5_000_000)
        
        self.prediction_count += 1
        
        return MLPrediction(
            predicted_views=predicted_views,
            confidence_low=confidence_low,
            confidence_high=confidence_high,
            probability_baseline=baseline_prob,
            model_version=self.model_version,
            prediction_timestamp=datetime.now()
        )
    
    def predict_batch(self, video_features: List[Tuple[str, Dict]]) -> List[MLPrediction]:
        """Generate mock batch predictions."""
        return [self.predict_single(video_id, features) for video_id, features in video_features]
    
    async def predict_async(self, video_id: str, features: Dict) -> MLPrediction:
        """Async mock prediction."""
        await asyncio.sleep(0.001)  # Simulate async processing
        return self.predict_single(video_id, features)
    
    def get_model_info(self) -> Dict:
        """Return mock model info."""
        return {
            'model_version': self.model_version,
            'model_type': 'mock_simulator',
            'training_date': datetime.now().isoformat(),
            'accuracy_metrics': {
                'mape': 0.25,  # Mock 25% mean absolute percentage error
                'r2_score': 0.75,
                'baseline_accuracy': 0.80
            },
            'predictions_made': self.prediction_count
        }


@dataclass
class LongTailDistribution:
    """Complete long-tail distribution analysis."""
    total_videos: int
    distribution_percentiles: Dict[str, float]  # 10th, 25th, 50th, 75th, 90th, 95th, 99th
    long_tail_segments: Dict[str, Dict]  # Segments: micro, small, medium, large, mega
    gini_coefficient: float  # Inequality measure
    power_law_alpha: float  # Power law exponent
    segment_counts: Dict[str, int]  # Count of videos per segment
    views_concentration: Dict[str, float]  # % of total views per segment

@dataclass
class TimeBucketMetrics:
    """Metrics for a specific time bucket (daily/weekly/monthly)."""
    bucket_type: str  # 'daily', 'weekly', 'monthly'
    bucket_start: datetime
    bucket_end: datetime
    total_videos: int
    total_views: int
    avg_views_per_video: float
    avg_engagement_rate: float
    baseline_compliance_rate: float
    videos_above_5m: int
    videos_above_10m: int
    videos_above_30m: int
    avg_growth_velocity: float
    avg_virality_score: float
    long_tail_distribution: LongTailDistribution
    top_performers: List[Dict]  # Top 10 videos in bucket
    worst_performers: List[Dict]  # Bottom 10 videos in bucket

@dataclass
class BoosterRequest:
    """Formal output contract for booster orchestration."""
    video_id: str
    booster_type: str  # 'immediate_boost', 'boost', 'repost', 'optimize'
    priority: int  # 1-10, lower number = higher priority
    urgency_score: float  # 0-1, how urgently action needed
    severity_score: float  # 0-100, intervention severity
    expected_impact: Dict[str, float]  # Expected improvements
    cooldown_until: datetime  # When this video can be boosted again
    retry_count: int  # Number of retry attempts
    max_retries: int  # Maximum allowed retries
    created_at: datetime
    expires_at: datetime  # When request expires
    metadata: Dict[str, Any]  # Additional context for orchestration

@dataclass
class BoosterQueue:
    """Priority queue for booster requests with cooldown management."""
    queue_name: str
    requests: List[BoosterRequest]
    max_queue_size: int
    processing_capacity: int  # Max requests per hour
    cooldown_rules: Dict[str, timedelta]  # Cooldown per booster type
    priority_weights: Dict[str, float]  # Priority multipliers
    
@dataclass
class BoosterResponse:
    """Response from booster execution with results."""
    request_id: str
    video_id: str
    booster_type: str
    status: str  # 'success', 'failed', 'retry_later', 'cooldown'
    execution_time: datetime
    completion_time: datetime
    results: Dict[str, Any]
    error_message: Optional[str]
    next_eligible_date: Optional[datetime]
    impact_measured: bool

@dataclass
class DashboardAPISchema:
    """Schema versioning for dashboard API compatibility."""
    schema_version: str
    api_endpoint: str
    authentication_required: bool
    supported_formats: List[str]
    field_mappings: Dict[str, str]
    deprecated_fields: List[str]
    required_fields: List[str]

@dataclass
class DashboardAPIResponse:
    """Response from dashboard API with status and metadata."""
    success: bool
    status_code: int
    response_time_ms: float
    message: str
    data_sent: Dict[str, Any]
    api_version: str
    timestamp: datetime
    error_details: Optional[str] = None

@dataclass
class FactoryKPIs:
    """Container for aggregated factory-level KPIs."""
    niche: str
    timestamp: datetime
    total_videos: int
    total_views: int
    avg_views_per_video: float
    avg_engagement_rate: float
    baseline_compliance_rate: float
    videos_above_5m: int
    videos_above_10m: int
    videos_above_30m: int
    avg_growth_velocity: float
    avg_virality_score: float
    long_tail_potential: float
    underperforming_count: int
    daily_upload_rate: float


class FactoryMetrics:
    """
    Calculates and tracks all key performance indicators (KPIs) for a specific niche factory.
    
    This is the central performance monitoring engine that:
    - Tracks real-time video metrics
    - Calculates engagement and virality scores
    - Identifies underperforming videos
    - Aggregates factory-level statistics
    - Triggers interventions for baseline compliance
    """
    
    def __init__(self, niche: str, data_dir: str, config: dict):
        """
        Initialize the metrics tracker for a factory.
        
        Args:
            niche: Niche name of the factory
            data_dir: Path to processed video metadata
            config: Niche-specific thresholds and KPI targets
        """
        self.niche = niche
        self.data_dir = Path(data_dir)
        self.config = config
        
        # KPI thresholds from config
        self.baseline_views = config.get('baseline_views', 5_000_000)
        self.target_engagement_rate = config.get('target_engagement_rate', 0.05)
        self.min_retention_rate = config.get('min_retention_rate', 0.45)
        self.intervention_threshold_days = config.get('intervention_days', 7)
        
        # Platform-specific weights
        self.platform_weights = config.get('platform_weights', {
            'youtube': {'likes': 1.0, 'comments': 2.0, 'shares': 3.0},
            'tiktok': {'likes': 0.8, 'comments': 1.5, 'shares': 2.5},
            'instagram': {'likes': 0.9, 'comments': 1.8, 'shares': 2.8},
            'facebook': {'likes': 1.2, 'comments': 2.2, 'shares': 3.2}  # Added facebook weights
        })
        
        # Initialize KPI Contract Manager (razor-sharp enforcement)
        self.kpi_contract_manager = KPIContractManager()
        
        # Initialize Early-Warning Micro-Signal Detector (pre-failure detection)
        self.micro_signal_detector = EarlyWarningMicroSignalDetector()
        
        # Initialize Confidence-Weighted Decision Engine (budget optimization)
        self.decision_engine = ConfidenceWeightedDecisionEngine()
        
        # Initialize Cross-Video Cannibalization Detector (self-sabotage prevention)
        self.cannibalization_detector = CannibalizationDetector()
        
        # Initialize Viral Anomaly Guardrails (fail open, never fail closed)
        self.viral_anomaly_detector = ViralAnomalyDetector()
        
        # Initialize Factory Learning Memory (compounding advantage system)
        self.learning_memory = FactoryLearningMemory()
        
        # Batch processing configuration for scaling
        self.batch_size = config.get('batch_size', 1000)  # Videos per batch
        
        # Booster queue configuration
        self.booster_config = config.get('booster_queues', {
            'cooldown_rules': {
                'immediate_boost': timedelta(hours=24),
                'boost': timedelta(days=3),
                'repost': timedelta(days=7),
                'optimize': timedelta(days=14)
            },
            'max_retries': {
                'immediate_boost': 3,
                'boost': 2,
                'repost': 2,
                'optimize': 1
            },
            'priority_weights': {
                'critical': 1.0,
                'high': 2.0,
                'medium': 3.0,
                'low': 4.0
            },
            'queue_capacity': {
                'immediate_boost': 50,
                'boost': 100,
                'repost': 200,
                'optimize': 500
            },
            'expiration_hours': 72  # Requests expire after 72 hours
        })
        
        # Initialize booster queues
        self.booster_queues = self._initialize_booster_queues()
        
        # Dashboard API configuration
        self.dashboard_config = config.get('dashboard_api', {
            'enabled': True,
            'base_url': 'http://localhost:8000/api/v1',
            'api_key': None,  # Set in environment or config
            'timeout_seconds': 30,
            'retry_attempts': 3,
            'schema_version': 'v1.2.0',
            'fallback_to_file': True
        })
        
        # Initialize dashboard API schema
        self.dashboard_schema = DashboardAPISchema(
            schema_version=self.dashboard_config['schema_version'],
            api_endpoint=f"{self.dashboard_config['base_url']}/metrics",
            authentication_required=True,
            supported_formats=['json'],
            field_mappings={
                'niche': 'niche',
                'timestamp': 'timestamp',
                'total_videos': 'total_videos',
                'total_views': 'total_views',
                'avg_views_per_video': 'avg_views_per_video',
                'avg_engagement_rate': 'avg_engagement_rate',
                'baseline_compliance_rate': 'baseline_compliance_rate',
                'videos_above_5m': 'videos_above_5m',
                'videos_above_10m': 'videos_above_10m',
                'videos_above_30m': 'videos_above_30m',
                'avg_growth_velocity': 'avg_growth_velocity',
                'avg_virality_score': 'avg_virality_score',
                'long_tail_potential': 'long_tail_potential',
                'underperforming_count': 'underperforming_count',
                'daily_upload_rate': 'daily_upload_rate'
            },
            deprecated_fields=[],
            required_fields=['niche', 'timestamp', 'total_videos', 'total_views']
        )
        
        # Scalability and distributed processing configuration
        self.scalability_config = config.get('scalability', {
            'streaming_enabled': True,
            'chunk_size': 10000,  # Process in 10k video chunks
            'use_columnar': True,  # Use columnar data structures
            'distributed_mode': False,  # Enable for distributed processing
            'parallel_workers': 4,  # Number of parallel workers
            'memory_limit_mb': 2048,  # Memory limit per worker
            'use_dask': False,  # Use Dask for distributed computing
            'use_ray': False,  # Use Ray for distributed processing
            'cache_strategy': 'lru',  # LRU cache for ML predictions
            'max_cache_size': 100000,  # Max cached predictions
            'batch_optimization': True,  # Enable batch optimizations
            'vectorized_ops': True,  # Use numpy vectorization
            'lazy_loading': True,  # Load data lazily for memory efficiency
            'compression': 'gzip',  # Compress intermediate data
            'checkpoint_interval': 100000,  # Checkpoint every 100k videos
        })
        
        # Initialize distributed processing hooks
        self.distributed_hooks = {
            'pre_batch_hook': None,
            'post_batch_hook': None,
            'error_handler': None,
            'progress_callback': None,
            'checkpoint_handler': None
        }
        
        # Initialize streaming data structures
        self.streaming_cache = {}
        self.columnar_dataframes = {}
        self.processing_stats = {
            'total_processed': 0,
            'processing_rate': 0.0,
            'memory_usage_mb': 0.0,
            'cache_hit_rate': 0.0,
            'error_count': 0
        }
        
        # Performance tracking
        self.batch_metrics = {
            'total_batches_processed': 0,
            'total_videos_processed': 0,
            'avg_processing_time_ms': 0.0,
            'peak_throughput_per_second': 0.0
        }
        
        # Load video statistics
        self.video_stats = self._load_video_stats()
        self.kpi_summary = {}
        
        # Cache for ML predictions
        self._ml_predictions_cache = {}
        
        # Initialize ML predictor (use mock for now, replace with real ML models)
        try:
            # Try to import real ML predictor
            from models.ml_models.engagement_predictor import EngagementPredictor
            self.ml_predictor = EngagementPredictor()
            print(f"[FactoryMetrics] Real ML predictor initialized")
        except ImportError:
            print(f"[FactoryMetrics] ML predictor not available, using mock predictor")
            self.ml_predictor = MockMLPredictor()
        
        # Initialize long-tail tracker if available
        self.long_tail_tracker = None
        if LONG_TAIL_AVAILABLE:
            try:
                self.long_tail_tracker = LongTailTracker(
                    niche_config=config,
                    data_dir=str(self.data_dir)
                )
                print(f"[FactoryMetrics] Long-tail tracker initialized for niche '{self.niche}'")
            except Exception as e:
                print(f"[FactoryMetrics] Error initializing long-tail tracker: {e}")
                self.long_tail_tracker = None
        
        print(f"[FactoryMetrics] Initialized for niche '{niche}' with {len(self.video_stats)} videos")
        print(f"[FactoryMetrics] KPI Contract Manager loaded with {len(self.kpi_contract_manager.contracts)} contracts")
        print(f"[FactoryMetrics] Early-Warning Micro-Signal Detector initialized")
        print(f"[FactoryMetrics] Confidence-Weighted Decision Engine initialized")
        print(f"[FactoryMetrics] Cross-Video Cannibalization Detector initialized")
        print(f"[FactoryMetrics] Viral Anomaly Guardrails initialized (FAIL OPEN, NEVER FAIL CLOSED)")
        print(f"[FactoryMetrics] Factory Learning Memory initialized (COMPOUNDING ADVANTAGE SYSTEM)")
        
        # Initialize logger
        self.logger = logging.getLogger(f'FactoryMetrics_{self.niche}')
    
    def detect_cannibalization_for_video(self, video_id: str) -> List:
        """
        Detect cannibalization patterns for a specific video.
        
        Args:
            video_id: Video identifier
            
        Returns:
            List[CannibalizationSignal]: Detected cannibalization signals
        """
        try:
            # Get video data
            video_data = self.video_stats
            
            # Detect cannibalization patterns
            signals = self.cannibalization_detector.detect_cannibalization(video_data, video_id, self.niche)
            
            # Generate alert if critical signals detected
            if signals:
                critical_signals = [s for s in signals if s.severity in ['critical', 'high']]
                if critical_signals:
                    alert = self.cannibalization_detector.generate_cannibalization_alert(critical_signals, self.niche)
                    if alert:
                        self.logger.warning(f"Cannibalization alert generated: {alert.recommendation}")
            
            return signals
            
        except Exception as e:
            self.logger.error(f"Error detecting cannibalization for {video_id}: {e}")
            return []
    
    def get_cannibalization_dashboard(self) -> Dict:
        """
        Get comprehensive cannibalization detection dashboard for the factory.
        
        Returns:
            Dict: Complete cannibalization analysis and prevention statistics
        """
        try:
            return self.cannibalization_detector.get_cannibalization_dashboard()
            
        except Exception as e:
            self.logger.error(f"Error getting cannibalization dashboard: {e}")
            return {'error': str(e)}
    
    def check_posting_safety(self, video_id: str) -> Dict:
        """
        Check if it's safe to post a new video based on cannibalization analysis.
        
        Args:
            video_id: Video identifier to check
            
        Returns:
            Dict: Safety assessment with recommendations
        """
        try:
            # Detect cannibalization
            signals = self.detect_cannibalization_for_video(video_id)
            
            if not signals:
                return {
                    'safe_to_post': True,
                    'risk_level': 'low',
                    'recommendation': 'Safe to proceed with posting',
                    'pause_required': False,
                    'pause_duration_hours': 0
                }
            
            # Determine risk level
            severities = [s.severity for s in signals]
            if 'critical' in severities:
                risk_level = 'critical'
                pause_required = True
                max_pause = max(s.pause_duration_hours for s in signals)
            elif 'high' in severities:
                risk_level = 'high'
                pause_required = True
                max_pause = max(s.pause_duration_hours for s in signals)
            elif 'medium' in severities:
                risk_level = 'medium'
                pause_required = True
                max_pause = max(s.pause_duration_hours for s in signals)
            else:
                risk_level = 'low'
                pause_required = False
                max_pause = 0
            
            # Generate recommendation
            if pause_required:
                recommendation = f"Pause posting in niche {self.niche} for {max_pause} hours due to cannibalization risk"
            else:
                recommendation = f"Monitor posting schedule, low cannibalization risk detected"
            
            return {
                'safe_to_post': not pause_required,
                'risk_level': risk_level,
                'recommendation': recommendation,
                'pause_required': pause_required,
                'pause_duration_hours': max_pause,
                'detected_signals': len(signals),
                'signal_types': list(set([s.cannibalization_type for s in signals])),
                'competing_videos': list(set([comp for s in signals for comp in s.competing_videos]))
            }
            
        except Exception as e:
            self.logger.error(f"Error checking posting safety for {video_id}: {e}")
            return {
                'safe_to_post': True,
                'risk_level': 'error',
                'recommendation': 'Unable to assess cannibalization risk',
                'pause_required': False,
                'pause_duration_hours': 0
            }
    
    def make_confidence_weighted_decision(self, 
                                        base_action: str,
                                        confidence: float,
                                        context: Dict = None) -> Dict:
        """
        Make a confidence-weighted decision for factory operations.
        
        Args:
            base_action: Base action type (boost, optimize, repost, etc.)
            confidence: ML confidence score (0-1)
            context: Additional context for decision making
            
        Returns:
            Dict: Decision details with all weighted parameters
        """
        try:
            # Get confidence-weighted decision
            decision = self.decision_engine.calculate_confidence_weighted_decision(base_action, confidence, context)
            
            # Convert to dictionary for API response
            decision_dict = {
                'action': decision.base_action,
                'intensity': decision.action_intensity,
                'confidence': decision.confidence,
                'priority': decision.weighted_priority,
                'severity': decision.weighted_severity,
                'budget_allocation': decision.budget_allocation,
                'retry_limit': decision.retry_limit,
                'cooldown_duration': str(decision.cooldown_duration),
                'expected_roi': decision.expected_roi,
                'risk_assessment': decision.risk_assessment,
                'decision_factors': decision.decision_factors,
                'timestamp': decision.timestamp.isoformat(),
                'expires_at': decision.expires_at.isoformat()
            }
            
            # Log the decision
            self.logger.info(f"Confidence-weighted decision made: {base_action} -> {decision.action_intensity} (confidence: {confidence:.2f})")
            
            return decision_dict
            
        except Exception as e:
            self.logger.error(f"Error making confidence-weighted decision: {e}")
            return {
                'error': str(e),
                'action': base_action,
                'confidence': confidence,
                'fallback_decision': 'manual_review_required'
            }
    
    def get_optimized_action_plan(self, 
                                 video_id: str, 
                                 ml_confidence: float, 
                                 recommended_action: str) -> Dict:
        """
        Get an optimized action plan based on ML confidence.
        
        Args:
            video_id: Video identifier
            ml_confidence: ML model confidence score (0-1)
            recommended_action: Recommended action from ML model
            
        Returns:
            Dict: Optimized action plan with confidence-weighted parameters
        """
        try:
            # Get video data for context
            video_data = self.video_stats[self.video_stats['video_id'] == video_id]
            
            if video_data.empty:
                return {
                    'error': 'Video not found',
                    'video_id': video_id,
                    'fallback_plan': 'manual_review_required'
                }
            
            # Build context for decision
            context = {
                'base_budget': 1000.0,  # Default budget
                'factory_performance': self.get_factory_performance_score(),
                'market_factor': 1.0,
                'video_performance': {
                    'current_views': video_data['views'].iloc[-1] if len(video_data) > 0 else 0,
                    'engagement_rate': (video_data['likes'].iloc[-1] / video_data['views'].iloc[-1]) if len(video_data) > 0 and video_data['views'].iloc[-1] > 0 else 0,
                    'days_since_publish': (datetime.now() - pd.to_datetime(video_data['publish_date'].iloc[0])).days if len(video_data) > 0 else 0
                }
            }
            
            # Make confidence-weighted decision
            decision = self.make_confidence_weighted_decision(recommended_action, ml_confidence, context)
            
            # Add video-specific information
            decision['video_id'] = video_id
            decision['ml_confidence'] = ml_confidence
            decision['recommended_action'] = recommended_action
            
            # Add budget optimization summary
            if 'budget_allocation' in decision:
                base_budget = context['base_budget']
                budget_efficiency = (decision['budget_allocation'] / base_budget) * 100
                decision['budget_efficiency'] = f"{budget_efficiency:.1f}%"
                
                if budget_efficiency < 50:
                    decision['budget_status'] = 'conservative_spending'
                elif budget_efficiency > 120:
                    decision['budget_status'] = 'aggressive_investment'
                else:
                    decision['budget_status'] = 'balanced_allocation'
            
            return decision
            
        except Exception as e:
            self.logger.error(f"Error creating optimized action plan for {video_id}: {e}")
            return {
                'error': str(e),
                'video_id': video_id,
                'fallback_plan': 'manual_review_required'
            }
    
    def get_factory_performance_score(self) -> float:
        """Get current factory performance score for decision context."""
        try:
            # Get factory health metrics
            factory_health = self.get_factory_health()
            
            # Calculate performance score based on baseline compliance
            baseline_compliance = factory_health.get('baseline_compliance_rate', 0.0)
            
            # Convert to performance factor (0.5 to 1.5 range)
            if baseline_compliance >= 0.8:
                return 1.2  # High performance
            elif baseline_compliance >= 0.6:
                return 1.0  # Normal performance
            elif baseline_compliance >= 0.4:
                return 0.8  # Below normal
            else:
                return 0.6  # Poor performance
                
        except Exception:
            return 1.0  # Default to normal performance
    
    def detect_viral_anomaly(self, video_id: str) -> Dict:
        """
        Detect viral anomalies using multi-signal confirmation.
        
        : FAIL OPEN, NEVER FAIL CLOSED - NEVER blocks real virality.
        
        Args:
            video_id: Video identifier
            
        Returns:
            Dict: Anomaly assessment with recommendations (never blocking)
        """
        try:
            # Get video data
            video_data = self.video_stats[self.video_stats['video_id'] == video_id]
            
            if video_data.empty:
                return {
                    'video_id': video_id,
                    'anomaly_probability': 0.1,
                    'recommendation': 'treat_as_real',
                    'quarantine_status': False,
                    'error': 'Video not found'
                }
            
            # Detect viral anomaly
            anomaly_score = self.viral_anomaly_detector.detect_viral_anomaly(video_data, video_id, self.niche)
            
            # Convert to dictionary for API response
            result = {
                'video_id': video_id,
                'anomaly_probability': anomaly_score.anomaly_probability,
                'confidence': anomaly_score.confidence,
                'recommendation': anomaly_score.recommendation,
                'quarantine_status': anomaly_score.quarantine_status,
                'signals_failed': anomaly_score.signals_failed,
                'signals_passed': anomaly_score.signals_passed,
                'success_override': anomaly_score.success_override,
                'shadow_booster_enabled': anomaly_score.shadow_booster_enabled,
                'last_evaluation': anomaly_score.last_evaluation.isoformat(),
                'organic_growth_allowed': True,  # NEVER block organic growth
                'expensive_boosters_blocked': anomaly_score.quarantine_status,
                'system_healthy': self.viral_anomaly_detector.system_healthy
            }
            
            # Log the assessment
            self.logger.info(f"Viral anomaly detection for {video_id}: "
                           f"probability={anomaly_score.anomaly_probability:.2f}, "
                           f"recommendation={anomaly_score.recommendation}")
            
            return result
            
        except Exception as e:
            self.logger.error(f"Error detecting viral anomaly for {video_id}: {e}")
            # : FAIL OPEN: Return optimistic assessment on system failure
            return {
                'video_id': video_id,
                'anomaly_probability': 0.1,
                'recommendation': 'treat_as_real',
                'quarantine_status': False,
                'error': str(e),
                'organic_growth_allowed': True,
                'expensive_boosters_blocked': False
            }
    
    def reevaluate_quarantined_video(self, video_id: str) -> Dict:
        """
        Re-evaluate a quarantined video for anomaly status.
        
        Args:
            video_id: Video identifier
            
        Returns:
            Dict: Updated anomaly assessment
        """
        try:
            # Get video data
            video_data = self.video_stats[self.video_stats['video_id'] == video_id]
            
            if video_data.empty:
                return {
                    'video_id': video_id,
                    'error': 'Video not found',
                    'recommendation': 'treat_as_real'
                }
            
            # Re-evaluate anomaly
            new_score = self.viral_anomaly_detector.reevaluate_quarantined_video(video_data, video_id, self.niche)
            
            return {
                'video_id': video_id,
                'anomaly_probability': new_score.anomaly_probability,
                'recommendation': new_score.recommendation,
                'quarantine_status': new_score.quarantine_status,
                'signals_failed': new_score.signals_failed,
                'signals_passed': new_score.signals_passed,
                'success_override': new_score.success_override,
                'last_evaluation': new_score.last_evaluation.isoformat()
            }
            
        except Exception as e:
            self.logger.error(f"Error re-evaluating quarantine for {video_id}: {e}")
            return {
                'video_id': video_id,
                'error': str(e),
                'recommendation': 'treat_as_real',
                'anomaly_probability': 0.1,
                'quarantine_status': False
            }
    
    def get_viral_anomaly_dashboard(self) -> Dict:
        """
        Get comprehensive viral anomaly detection dashboard.
        
        Returns:
            Dict: Complete anomaly analysis and safety metrics
        """
        try:
            return self.viral_anomaly_detector.get_anomaly_dashboard()
            
        except Exception as e:
            self.logger.error(f"Error getting viral anomaly dashboard: {e}")
            return {'error': str(e)}
    
    def check_booster_safety_for_video(self, video_id: str, booster_type: str, budget: float) -> Dict:
        """
        Check if it's safe to apply a booster based on anomaly assessment.
        
        : NEVER blocks organic growth, only provides safety guidance.
        
        Args:
            video_id: Video identifier
            booster_type: Type of booster being considered
            budget: Budget amount for the booster
            
        Returns:
            Dict: Safety assessment with recommendations
        """
        try:
            # Get anomaly assessment
            anomaly_result = self.detect_viral_anomaly(video_id)
            
            if 'error' in anomaly_result:
                return {
                    'video_id': video_id,
                    'booster_type': booster_type,
                    'budget': budget,
                    'safe_to_boost': True,  # : FAIL OPEN
                    'recommendation': 'Proceed with booster (anomaly detection unavailable)',
                    'risk_level': 'unknown',
                    'anomaly_probability': 0.1
                }
            
            anomaly_probability = anomaly_result['anomaly_probability']
            recommendation = anomaly_result['recommendation']
            quarantine_status = anomaly_result['quarantine_status']
            
            # Safety assessment based on anomaly probability
            if recommendation == 'treat_as_real':
                safe_to_boost = True
                risk_level = 'low'
                booster_recommendation = f"Safe to apply {booster_type} booster"
            elif recommendation == 'monitor':
                safe_to_boost = True  # Still allow, but with caution
                risk_level = 'medium'
                booster_recommendation = f"Monitor {booster_type} booster performance closely"
            elif recommendation == 'delay_spend':
                safe_to_boost = budget <= 100  # Allow small boosters
                risk_level = 'high'
                booster_recommendation = f"Delay expensive {booster_type} booster, consider smaller budget"
            else:  # flag_for_review
                safe_to_boost = False  # Recommend against expensive boosters
                risk_level = 'critical'
                booster_recommendation = f"Flag {booster_type} booster for human review before spending"
            
            # Quarantine affects expensive boosters
            if quarantine_status and budget > 500:
                safe_to_boost = False
                booster_recommendation = f"Video under quarantine - delay {booster_type} booster until cleared"
            
            return {
                'video_id': video_id,
                'booster_type': booster_type,
                'budget': budget,
                'safe_to_boost': safe_to_boost,
                'risk_level': risk_level,
                'recommendation': booster_recommendation,
                'anomaly_probability': anomaly_probability,
                'quarantine_status': quarantine_status,
                'organic_growth_allowed': True,  # NEVER blocks organic growth
                'shadow_booster_suggested': anomaly_result.get('shadow_booster_enabled', False)
            }
            
        except Exception as e:
            self.logger.error(f"Error checking booster safety for {video_id}: {e}")
            # : FAIL OPEN: Allow booster on error
            return {
                'video_id': video_id,
                'booster_type': booster_type,
                'budget': budget,
                'safe_to_boost': True,
                'recommendation': f"Proceed with {booster_type} booster (safety check failed)",
                'risk_level': 'unknown',
                'anomaly_probability': 0.1
            }
    
    def record_action_outcome(self, 
                            action_type: str,
                            video_id: str,
                            lifecycle_stage: str,
                            pre_metrics: Dict[str, float],
                            post_metrics: Dict[str, float],
                            action_parameters: Dict[str, Any],
                            duration_days: int = 7) -> str:
        """
        Record the outcome of a factory action for learning.
        
        Args:
            action_type: Type of action taken ('boost', 'optimize', 'repost', 'immediate_boost')
            video_id: Video identifier
            lifecycle_stage: Current lifecycle stage ('ignition', 'growth', 'maturity', 'decline')
            pre_metrics: Metrics before action
            post_metrics: Metrics after action
            action_parameters: Parameters used in action
            duration_days: How long to track the outcome
            
        Returns:
            str: Action memory ID for tracking
        """
        return self.learning_memory.record_action_outcome(
            action_type=action_type,
            niche=self.niche,
            video_id=video_id,
            lifecycle_stage=lifecycle_stage,
            pre_metrics=pre_metrics,
            post_metrics=post_metrics,
            action_parameters=action_parameters,
            duration_days=duration_days
        )
    
    def get_action_recommendations(self, lifecycle_stage: str, current_metrics: Dict[str, float]) -> Dict:
        """
        Get action recommendations based on learning memory.
        
        Args:
            lifecycle_stage: Current lifecycle stage
            current_metrics: Current video metrics
            
        Returns:
            Dict: Action recommendations with confidence scores
        """
        return self.learning_memory.get_action_recommendations(
            niche=self.niche,
            lifecycle_stage=lifecycle_stage,
            current_metrics=current_metrics
        )
    
    def get_learning_dashboard(self) -> Dict:
        """
        Get comprehensive learning dashboard for the factory.
        
        Returns:
            Dict: Complete learning analysis and insights
        """
        return self.learning_memory.get_learning_dashboard()
    
    def export_learning_data(self, file_path: str) -> bool:
        """
        Export learning data for backup or analysis.
        
        Args:
            file_path: Path to save the learning data
            
        Returns:
            bool: Success status
        """
        return self.learning_memory.export_learning_data(file_path)
    
    def import_learning_data(self, file_path: str) -> bool:
        """
        Import learning data from backup.
        
        Args:
            file_path: Path to the learning data file
            
        Returns:
            bool: Success status
        """
        return self.learning_memory.import_learning_data(file_path)
    
    def get_lifecycle_stage_for_video(self, video_id: str) -> str:
        """
        Determine the lifecycle stage for a video.
        
        Args:
            video_id: Video identifier
            
        Returns:
            str: Lifecycle stage ('ignition', 'growth', 'maturity', 'decline')
        """
        try:
            # Get video data
            video_data = self.video_stats[self.video_stats['video_id'] == video_id]
            
            if video_data.empty:
                return 'ignition'  # Default for unknown videos
            
            # Get current metrics
            latest_data = video_data.iloc[-1]
            views = latest_data['views']
            days_since_publish = (datetime.now() - pd.to_datetime(latest_data['publish_date'])).days
            
            # Calculate growth velocity
            if len(video_data) >= 2:
                first_data = video_data.iloc[0]
                velocity = (views - first_data['views']) / max(1, days_since_publish)
            else:
                velocity = 0
            
            # Determine lifecycle stage
            if days_since_publish <= 3:
                return 'ignition'
            elif days_since_publish <= 14 and velocity > 1000:
                return 'growth'
            elif days_since_publish <= 30:
                return 'maturity'
            else:
                return 'decline'
                
        except Exception as e:
            self.logger.error(f"Error determining lifecycle stage for {video_id}: {e}")
            return 'ignition'
    
    def get_compounding_advantage_score(self) -> float:
        """
        Get the compounding advantage score for this factory.
        
        Returns:
            float: Compounding advantage multiplier (1.0 = no advantage, >1.0 = advantage)
        """
        return self.learning_memory._calculate_compounding_advantage(self.niche)
    
    def detect_early_warning_signals(self, video_id: str) -> List[MicroSignal]:
        """
        Detect early-warning micro-signals for a specific video.
        
        Args:
            video_id: Video identifier
            
        Returns:
            List[MicroSignal]: Detected micro-signals with recommendations
        """
        try:
            # Get video data
            video_data = self.video_stats[self.video_stats['video_id'] == video_id]
            
            if video_data.empty:
                return []
            
            # Detect micro-signals
            signals = self.micro_signal_detector.detect_micro_signals(video_data, video_id)
            
            return signals
            
        except Exception as e:
            print(f"[FactoryMetrics] Error detecting micro-signals for {video_id}: {e}")
            return []
    
    def get_comprehensive_factory_health(self) -> Dict:
        """
        Get comprehensive factory health including all razor-sharp features.
        
        Returns:
            Dict: Complete factory health with KPI contracts, anomalies, and micro-signals
        """
        try:
            # Get factory health with contracts
            factory_health = self.get_factory_health_with_contracts()
            
            # Get micro-signal summary
            signal_report = self.micro_signal_detector.get_factory_signal_report()
            
            # Add micro-signal information
            factory_health['micro_signals'] = signal_report
            factory_health['early_warning_status'] = self._determine_early_warning_status(signal_report)
            
            # Calculate comprehensive health score
            base_score = factory_health['overall_health_score']
            
            # Apply micro-signal penalty
            if signal_report['critical_signals'] > 0:
                micro_signal_penalty = 0.2 * (signal_report['critical_signals'] / max(1, signal_report['total_signals']))
                factory_health['comprehensive_health_score'] = max(0.0, base_score - micro_signal_penalty)
            else:
                factory_health['comprehensive_health_score'] = base_score
            
            return factory_health
            
        except Exception as e:
            print(f"[FactoryMetrics] Error getting comprehensive factory health: {e}")
            return {'error': str(e)}
    
    def _determine_early_warning_status(self, signal_report: Dict) -> str:
        """Determine early warning status based on micro-signals."""
        critical_signals = signal_report.get('critical_signals', 0)
        total_signals = signal_report.get('total_signals', 0)
        auto_actions = signal_report.get('auto_actions_triggered', 0)
        
        if critical_signals > 0:
            return 'CRITICAL_EARLY_WARNINGS'
        elif auto_actions > 0:
            return 'PRE_BOOSTS_TRIGGERED'
        elif total_signals > 5:
            return 'MULTIPLE_MICRO_SIGNALS'
        elif total_signals > 0:
            return 'MINOR_SIGNALS_DETECTED'
        else:
            return 'NO_EARLY_WARNINGS'
    
    def compute_per_video_metrics_with_contracts(self, video_id: str) -> Tuple[Optional[VideoKPIs], Dict]:
        """
        Compute KPIs with automatic KPI contract validation.
        
        Returns:
            Tuple[VideoKPIs, Dict]: KPIs and contract validation results
        """
        # Compute standard KPIs
        kpi = self.compute_per_video_metrics(video_id)
        
        if not kpi:
            return None, {'error': 'KPI computation failed'}
        
        # Prepare KPIs for contract validation
        kpis_for_validation = {
            'engagement_rate_floor': kpi.engagement_rate,
            'engagement_rate_ceiling': kpi.engagement_rate,
            'retention_rate_floor': kpi.avg_retention_rate,
            'velocity_minimum': kpi.growth_velocity,
            'virality_minimum': kpi.virality_score
        }
        
        # Add velocity deadline check if video is old enough
        if kpi.days_since_publish >= 30:
            kpis_for_validation['velocity_deadline'] = kpi.total_views / 30
        
        # Validate against contracts
        contract_results = self.kpi_contract_manager.validate_all_kpis(
            kpis_for_validation, 
            video_id=video_id, 
            factory_id=self.niche
        )
        
        return kpi, contract_results
    
    def get_factory_health_with_contracts(self) -> Dict:
        """
        Get factory health with KPI contract compliance.
        
        Returns:
            Dict: Factory health with contract compliance status
        """
        # Get standard factory health
        factory_kpis = self.aggregate_factory_metrics()
        
        # Get contract compliance report
        compliance_report = self.kpi_contract_manager.get_compliance_report()
        
        # Calculate factory-wide KPIs for contract validation
        factory_kpis_for_contracts = {
            'baseline_compliance': factory_kpis.baseline_compliance_rate / 100
        }
        
        # Validate factory-wide contracts
        factory_contract_results = self.kpi_contract_manager.validate_all_kpis(
            factory_kpis_for_contracts,
            factory_id=self.niche
        )
        
        return {
            'factory_kpis': asdict(factory_kpis),
            'contract_compliance': compliance_report,
            'factory_contract_results': factory_contract_results,
            'overall_health_score': self._calculate_enhanced_health_score(factory_kpis, compliance_report),
            'governance_status': self._determine_governance_status(compliance_report, factory_contract_results),
            'immediate_actions': self._get_immediate_actions(compliance_report, factory_contract_results)
        }
    
    def _calculate_enhanced_health_score(self, factory_kpis, compliance_report) -> float:
        """Calculate enhanced health score with contract compliance."""
        base_health = (
            0.4 * (factory_kpis.baseline_compliance_rate / 100) +
            0.3 * min(1.0, factory_kpis.avg_engagement_rate / (self.target_engagement_rate * 2)) +
            0.2 * min(1.0, factory_kpis.avg_growth_velocity / (self.baseline_views / 30)) +
            0.1 * min(1.0, factory_kpis.long_tail_potential)
        )
        
        # Apply contract compliance penalty
        compliance_penalty = 1.0 - compliance_report['compliance_rate']
        
        return max(0.0, base_health - (compliance_penalty * 0.5))
    
    def _determine_governance_status(self, compliance_report, factory_contract_results) -> str:
        """Determine overall governance status."""
        if compliance_report['critical_violations'] > 0 or factory_contract_results['critical_violations']:
            return 'CRITICAL_GOVERNANCE_FAILURE'
        elif compliance_report['total_violations'] > 3:
            return 'MULTIPLE_CONTRACT_VIOLATIONS'
        elif compliance_report['total_violations'] > 0:
            return 'MINOR_CONTRACT_VIOLATIONS'
        else:
            return 'FULL_COMPLIANCE'
    
    def _get_immediate_actions(self, compliance_report, factory_contract_results) -> List[str]:
        """Get immediate actions based on contract violations."""
        actions = []
        
        if compliance_report['critical_violations'] > 0:
            actions.append('🚨 IMMEDIATE: Review critical KPI contract violations')
        
        if factory_contract_results['critical_violations']:
            actions.append('🚨 IMMEDIATE: Factory-wide contract enforcement required')
        
        # Add specific actions based on violation categories
        violations_by_category = compliance_report.get('violations_by_category', {})
        
        if violations_by_category.get('engagement', 0) > 0:
            actions.append('⚠️ Engagement rate contracts violated - review content strategy')
        
        if violations_by_category.get('retention', 0) > 0:
            actions.append('⚠️ Retention rate contracts violated - improve content quality')
        
        if violations_by_category.get('velocity', 0) > 0:
            actions.append('⚠️ Velocity contracts violated - boost distribution strategy')
        
        if violations_by_category.get('baseline', 0) > 0:
            actions.append('🚨 Baseline contracts violated - emergency intervention required')
        
        return actions if actions else ['✅ All contracts compliant']
    
    def _load_video_stats_streaming(self) -> pd.DataFrame:
        """
        Load video statistics with streaming I/O for large datasets.
        
        Uses chunked reading and columnar optimizations for memory efficiency.
        """
        try:
            metadata_dir = self.data_dir / self.niche / "metadata"
            
            if not metadata_dir.exists():
                print(f"[FactoryMetrics] Creating metadata directory: {metadata_dir}")
                metadata_dir.mkdir(parents=True, exist_ok=True)
                return pd.DataFrame()
            
            # Find all data files
            parquet_files = list(metadata_dir.glob("*.parquet"))
            csv_files = list(metadata_dir.glob("*.csv"))
            
            if not parquet_files and not csv_files:
                print(f"[FactoryMetrics] No video stats found for niche '{self.niche}'")
                return pd.DataFrame()
            
            # Use streaming I/O for large files
            if self.scalability_config['streaming_enabled']:
                return self._stream_load_data_files(parquet_files, csv_files)
            else:
                return self._traditional_load_data_files(parquet_files, csv_files)
            
        except Exception as e:
            print(f"[FactoryMetrics] Error loading video stats: {e}")
            return pd.DataFrame()
    
    def _stream_load_data_files(self, parquet_files: List[Path], csv_files: List[Path]) -> pd.DataFrame:
        """Load data files using streaming I/O with chunked processing."""
        try:
            dfs = []
            chunk_size = self.scalability_config['chunk_size']
            
            # Process parquet files with streaming
            for f in parquet_files:
                try:
                    # Use pyarrow for efficient streaming
                    if self.scalability_config['use_columnar']:
                        # Use columnar format for memory efficiency
                        import pyarrow.parquet as pq
                        table = pq.read_table(f)
                        df = table.to_pandas()
                    else:
                        # Traditional pandas with chunking
                        df = pd.read_parquet(f, engine='pyarrow')
                    
                    dfs.append(df)
                except Exception as e:
                    print(f"[FactoryMetrics] Error reading {f}: {e}")
            
            # Process CSV files with streaming
            for f in csv_files:
                try:
                    # Read CSV in chunks for memory efficiency
                    chunks = []
                    for chunk in pd.read_csv(f, chunksize=chunk_size):
                        chunks.append(chunk)
                    
                    if chunks:
                        df = pd.concat(chunks, ignore_index=True)
                        dfs.append(df)
                except Exception as e:
                    print(f"[FactoryMetrics] Error reading {f}: {e}")
            
            if not dfs:
                return pd.DataFrame()
            
            # Combine with columnar optimization
            if self.scalability_config['use_columnar'] and len(dfs) > 1:
                return self._combine_columnar_dataframes(dfs)
            else:
                combined_df = pd.concat(dfs, ignore_index=True)
                return self._optimize_dataframe(combined_df)
            
        except Exception as e:
            print(f"[FactoryMetrics] Error in streaming load: {e}")
            return pd.DataFrame()
    
    def _traditional_load_data_files(self, parquet_files: List[Path], csv_files: List[Path]) -> pd.DataFrame:
        """Traditional data loading without streaming (fallback)."""
        try:
            dfs = []
            
            for f in parquet_files:
                try:
                    df = pd.read_parquet(f)
                    dfs.append(df)
                except Exception as e:
                    print(f"[FactoryMetrics] Error reading {f}: {e}")
            
            for f in csv_files:
                try:
                    df = pd.read_csv(f)
                    dfs.append(df)
                except Exception as e:
                    print(f"[FactoryMetrics] Error reading {f}: {e}")
            
            if not dfs:
                return pd.DataFrame()
            
            combined_df = pd.concat(dfs, ignore_index=True)
            return self._optimize_dataframe(combined_df)
            
        except Exception as e:
            print(f"[FactoryMetrics] Error in traditional load: {e}")
            return pd.DataFrame()
    
    def _optimize_dataframe(self, df: pd.DataFrame) -> pd.DataFrame:
        """Optimize dataframe for memory efficiency and performance."""
        try:
            # Optimize data types
            df = df.copy()
            
            # Convert to optimal data types
            if 'views' in df.columns:
                df['views'] = pd.to_numeric(df['views'], downcast='integer')
            if 'likes' in df.columns:
                df['likes'] = pd.to_numeric(df['likes'], downcast='integer')
            if 'comments' in df.columns:
                df['comments'] = pd.to_numeric(df['comments'], downcast='integer')
            if 'shares' in df.columns:
                df['shares'] = pd.to_numeric(df['shares'], downcast='integer')
            if 'retention_rate' in df.columns:
                df['retention_rate'] = pd.to_numeric(df['retention_rate'], downcast='float')
            if 'watch_time_hours' in df.columns:
                df['watch_time_hours'] = pd.to_numeric(df['watch_time_hours'], downcast='float')
            
            # Convert timestamps
            for col in ['timestamp', 'publish_date']:
                if col in df.columns:
                    df[col] = pd.to_datetime(df[col])
            
            # Set default values efficiently
            if 'platform' not in df.columns:
                df['platform'] = 'youtube'
            if 'retention_rate' not in df.columns:
                df['retention_rate'] = 0.5
            if 'watch_time_hours' not in df.columns:
                df['watch_time_hours'] = df['views'] * 0.05
            
            return df
            
        except Exception as e:
            print(f"[FactoryMetrics] Error optimizing dataframe: {e}")
            return df
    
    def _combine_columnar_dataframes(self, dfs: List[pd.DataFrame]) -> pd.DataFrame:
        """Combine dataframes using columnar optimizations."""
        try:
            if not dfs:
                return pd.DataFrame()
            
            # Use pandas concat with optimization
            combined_df = pd.concat(dfs, ignore_index=True)
            
            # Apply columnar optimizations
            if self.scalability_config['use_columnar']:
                # Convert to categorical for repeated string columns
                if 'platform' in combined_df.columns:
                    combined_df['platform'] = combined_df['platform'].astype('category')
                if 'video_id' in combined_df.columns:
                    combined_df['video_id'] = combined_df['video_id'].astype('category')
            
            return combined_df
            
        except Exception as e:
            print(f"[FactoryMetrics] Error combining columnar dataframes: {e}")
            return pd.DataFrame()
    
    def _load_video_stats(self) -> pd.DataFrame:
        """
        Load historical and real-time video metrics from CSV/Parquet files.
        
        Returns:
            pd.DataFrame: Columns include video_id, timestamp, views, likes, 
                         comments, shares, retention, watch_time
        """
        try:
            metadata_dir = self.data_dir / self.niche / "metadata"
            
            if not metadata_dir.exists():
                print(f"[FactoryMetrics] Creating metadata directory: {metadata_dir}")
                metadata_dir.mkdir(parents=True, exist_ok=True)
                return pd.DataFrame()
            
            parquet_files = list(metadata_dir.glob("*.parquet"))
            csv_files = list(metadata_dir.glob("*.csv"))
            
            dfs = []
            
            if parquet_files:
                for f in parquet_files:
                    try:
                        df = pd.read_parquet(f)
                        dfs.append(df)
                    except Exception as e:
                        print(f"[FactoryMetrics] Error reading {f}: {e}")
            
            if csv_files:
                for f in csv_files:
                    try:
                        df = pd.read_csv(f)
                        dfs.append(df)
                    except Exception as e:
                        print(f"[FactoryMetrics] Error reading {f}: {e}")
            
            if not dfs:
                print(f"[FactoryMetrics] No video stats found for niche '{self.niche}'")
                return pd.DataFrame()
            
            combined_df = pd.concat(dfs, ignore_index=True)
            
            required_cols = ['video_id', 'timestamp', 'views', 'likes', 
                           'comments', 'shares', 'platform']
            
            for col in required_cols:
                if col not in combined_df.columns:
                    if col == 'timestamp':
                        combined_df[col] = datetime.now()
                    elif col == 'platform':
                        combined_df[col] = 'youtube'
                    elif col == 'publish_date':
                        combined_df[col] = combined_df['timestamp']
                    else:
                        combined_df[col] = 0
            
            if 'retention_rate' not in combined_df.columns:
                # Set default retention rate instead of random fallback for production
                combined_df['retention_rate'] = 0.5
            
            if 'watch_time_hours' not in combined_df.columns:
                combined_df['watch_time_hours'] = combined_df['views'] * 0.05
            
            for col in ['timestamp', 'publish_date']:
                if col in combined_df.columns:
                    combined_df[col] = pd.to_datetime(combined_df[col])
            
            # Preserve temporal data - DO NOT drop duplicates
            # combined_df = combined_df.drop_duplicates(subset=['video_id'], keep='last')
            # This line is commented out to preserve growth curves for real-time analysis
            
            print(f"[FactoryMetrics] Loaded {len(combined_df)} unique videos")
            
            return combined_df
            
        except Exception as e:
            print(f"[FactoryMetrics] Error loading video stats: {e}")
            return pd.DataFrame()
    
    def _calculate_engagement_rate(
        self, 
        views: int, 
        likes: int, 
        comments: int, 
        shares: int,
        platform: str = 'youtube'
    ) -> float:
        """Calculate weighted engagement rate normalized per platform."""
        if views == 0:
            return 0.0
        
        weights = self.platform_weights.get(platform.lower(), 
                                           self.platform_weights['youtube'])
        
        weighted_engagement = (
            likes * weights['likes'] +
            comments * weights['comments'] +
            shares * weights['shares']
        )
        
        engagement_rate = weighted_engagement / views
        
        return round(engagement_rate, 4)
    
    def _calculate_growth_velocity(
        self,
        video_id: str,
        current_views: int,
        publish_date: datetime
    ) -> float:
        """Compute derivative of views over time using preserved temporal data."""
        try:
            now = datetime.now()
            days_live = max(1, (now - publish_date).days)
            
            # Base velocity from current state
            velocity = current_views / days_live
            
            # Use preserved temporal data for accurate velocity calculation
            if video_id in self.video_stats['video_id'].values:
                video_history = self.video_stats[
                    self.video_stats['video_id'] == video_id
                ].sort_values('timestamp')
                
                if len(video_history) > 1:
                    # Calculate velocity from most recent data point
                    latest_record = video_history.iloc[-1]
                    previous_record = video_history.iloc[-2]
                    
                    latest_views = latest_record['views']
                    previous_views = previous_record['views']
                    
                    time_diff = (latest_record['timestamp'] - previous_record['timestamp']).total_seconds() / 86400  # Convert to days
                    
                    if time_diff > 0:
                        # True time-series derivative
                        recent_velocity = (latest_views - previous_views) / time_diff
                        # Weight recent velocity more heavily for real-time responsiveness
                        velocity = 0.7 * recent_velocity + 0.3 * velocity
                    
                    # Calculate acceleration (second derivative) if enough data points
                    if len(video_history) > 2:
                        older_record = video_history.iloc[-3]
                        older_views = older_record['views']
                        
                        time_diff_2 = (previous_record['timestamp'] - older_record['timestamp']).total_seconds() / 86400
                        
                        if time_diff_2 > 0:
                            velocity_2 = (previous_views - older_views) / time_diff_2
                            acceleration = recent_velocity - velocity_2
                            
                            # Adjust velocity based on acceleration trend
                            if acceleration > 0:
                                velocity *= 1.1  # Boost for accelerating growth
                            elif acceleration < -1000:  # Significant deceleration
                                velocity *= 0.9  # Reduce for declining growth
            
            return round(velocity, 2)
            
        except Exception as e:
            print(f"[FactoryMetrics] Error calculating velocity for {video_id}: {e}")
            return current_views / max(1, (datetime.now() - publish_date).days)
    
    def _calculate_virality_score(
        self,
        video_id: str,
        views: int,
        engagement_rate: float,
        growth_velocity: float,
        retention_rate: float,
        days_live: int
    ) -> float:
        """Calculate comprehensive virality score combining multiple signals."""
        try:
            view_score = min(40, (views / self.baseline_views) * 20)
            engagement_score = min(20, (engagement_rate / self.target_engagement_rate) * 20)
            
            expected_daily_views = self.baseline_views / 30
            velocity_score = min(30, (growth_velocity / expected_daily_views) * 30)
            
            retention_score = min(20, (retention_rate / self.min_retention_rate) * 20)
            
            time_factor = 1.0
            if days_live < 7:
                time_factor = 1.2
            elif days_live > 60:
                time_factor = 0.8
            
            base_virality = (
                view_score +
                engagement_score +
                velocity_score +
                retention_score
            ) * time_factor
            
            ml_boost = 0.0
            if video_id in self._ml_predictions_cache:
                prediction = self._ml_predictions_cache[video_id]
                if isinstance(prediction, MLPrediction):
                    predicted_views = prediction.predicted_views
                    # Use confidence interval for more robust boost calculation
                    confidence_factor = 1.0
                    if prediction.confidence_low > self.baseline_views:
                        confidence_factor = 1.2  # Boost for confident predictions
                    elif prediction.confidence_high < self.baseline_views:
                        confidence_factor = 0.8  # Reduce boost for uncertain predictions
                    
                    if predicted_views > self.baseline_views:
                        ml_boost = min(15, (predicted_views / (self.baseline_views * 2)) * 15 * confidence_factor)
                else:
                    # Legacy compatibility for old cache format
                    predicted_views = prediction
                    if predicted_views > self.baseline_views:
                        ml_boost = min(15, (predicted_views / (self.baseline_views * 2)) * 15)
            
            long_tail_boost = 0.0
            if self.long_tail_tracker:
                try:
                    long_tail_score = self.long_tail_tracker.compute_long_tail_score(video_id)
                    # Convert long_tail_score (0-1) to boost (0-15 points)
                    long_tail_boost = long_tail_score * 15
                except Exception as e:
                    print(f"[FactoryMetrics] Error getting long-tail score for {video_id}: {e}")
                    # Fallback to heuristic
                    if days_live > 30 and growth_velocity > self.long_tail_velocity_threshold:
                        long_tail_boost = 10.0
            else:
                # Fallback heuristic when long-tail tracker unavailable
                if days_live > 30 and growth_velocity > self.long_tail_velocity_threshold:
                    long_tail_boost = 10.0
            
            virality_score = base_virality + ml_boost + long_tail_boost
            virality_score = min(100, virality_score)
            
            return round(virality_score, 2)
            
        except Exception as e:
            print(f"[FactoryMetrics] Error calculating virality for {video_id}: {e}")
            return 50.0
    
    def _load_ml_predictions(self, video_ids: List[str]) -> Dict[str, MLPrediction]:
        """Load ML-predicted final view counts for videos using real predictor interface."""
        try:
            predictions = {}
            
            # Prepare features for batch prediction
            video_features = []
            for video_id in video_ids:
                if video_id in self.video_stats['video_id'].values:
                    video = self.video_stats[self.video_stats['video_id'] == video_id].iloc[0]
                    
                    # Extract features for ML model
                    features = {
                        'views': int(video['views']),
                        'likes': int(video['likes']),
                        'comments': int(video['comments']),
                        'shares': int(video['shares']),
                        'retention_rate': float(video.get('retention_rate', 0.5)),
                        'engagement_rate': self._calculate_engagement_rate(
                            int(video['views']), int(video['likes']), 
                            int(video['comments']), int(video['shares']),
                            str(video.get('platform', 'youtube'))
                        ),
                        'growth_velocity': self._calculate_growth_velocity(
                            video_id, int(video['views']), 
                            pd.to_datetime(video['publish_date'])
                        ),
                        'days_live': max(1, (datetime.now() - pd.to_datetime(video['publish_date'])).days),
                        'platform': str(video.get('platform', 'youtube')),
                        'publish_date': pd.to_datetime(video['publish_date']).isoformat()
                    }
                    
                    video_features.append((video_id, features))
            
            # Use batch prediction for efficiency
            if video_features:
                batch_predictions = self.ml_predictor.predict_batch(video_features)
                
                for (video_id, _), prediction in zip(video_features, batch_predictions):
                    predictions[video_id] = prediction
            
            # Update cache with full prediction objects (not just view counts)
            self._ml_predictions_cache.update(predictions)
            
            return predictions
            
        except Exception as e:
            print(f"[FactoryMetrics] Error loading ML predictions: {e}")
            return {}
    
    async def _load_ml_predictions_async(self, video_ids: List[str]) -> Dict[str, MLPrediction]:
        """Async version of ML predictions loading for non-blocking operations."""
        try:
            predictions = {}
            
            # Prepare features for async batch prediction
            tasks = []
            for video_id in video_ids:
                if video_id in self.video_stats['video_id'].values:
                    video = self.video_stats[self.video_stats['video_id'] == video_id].iloc[0]
                    
                    features = {
                        'views': int(video['views']),
                        'likes': int(video['likes']),
                        'comments': int(video['comments']),
                        'shares': int(video['shares']),
                        'retention_rate': float(video.get('retention_rate', 0.5)),
                        'engagement_rate': self._calculate_engagement_rate(
                            int(video['views']), int(video['likes']), 
                            int(video['comments']), int(video['shares']),
                            str(video.get('platform', 'youtube'))
                        ),
                        'growth_velocity': self._calculate_growth_velocity(
                            video_id, int(video['views']), 
                            pd.to_datetime(video['publish_date'])
                        ),
                        'days_live': max(1, (datetime.now() - pd.to_datetime(video['publish_date'])).days),
                        'platform': str(video.get('platform', 'youtube'))
                    }
                    
                    tasks.append(self.ml_predictor.predict_async(video_id, features))
            
            # Wait for all async predictions
            if tasks:
                batch_results = await asyncio.gather(*tasks)
                
                for i, video_id in enumerate(video_ids):
                    predictions[video_id] = batch_results[i]
            
            # Update cache
            self._ml_predictions_cache.update(predictions)
            
            return predictions
            
        except Exception as e:
            print(f"[FactoryMetrics] Error loading async ML predictions: {e}")
            return {}
    
    def get_ml_model_info(self) -> Dict:
        """Get information about the current ML model."""
        try:
            return self.ml_predictor.get_model_info()
        except Exception as e:
            print(f"[FactoryMetrics] Error getting ML model info: {e}")
            return {}
    
    def _vectorized_calculate_engagement_rates(self, df: pd.DataFrame) -> pd.Series:
        """Calculate engagement rates for all videos using vectorized operations."""
        if self.use_vectorized_ops and len(df) > 100:
            # Vectorized calculation for large datasets
            engagement_rates = []
            for _, row in df.iterrows():
                weights = self.platform_weights.get(row['platform'].lower(), 
                                                   self.platform_weights['youtube'])
                weighted_engagement = (
                    row['likes'] * weights['likes'] +
                    row['comments'] * weights['comments'] +
                    row['shares'] * weights['shares']
                )
                engagement_rate = weighted_engagement / row['views'] if row['views'] > 0 else 0.0
                engagement_rates.append(round(engagement_rate, 4))
            return pd.Series(engagement_rates, index=df.index)
        else:
            # Fallback to row-wise calculation
            return df.apply(lambda row: self._calculate_engagement_rate(
                row['views'], row['likes'], row['comments'], row['shares'], row['platform']
            ), axis=1)
    
    def _vectorized_calculate_growth_velocities(self, df: pd.DataFrame) -> pd.Series:
        """Calculate growth velocities for all videos using vectorized operations."""
        if self.use_vectorized_ops and len(df) > 100:
            # Vectorized calculation
            now = datetime.now()
            df_copy = df.copy()
            df_copy['days_live'] = (now - df_copy['publish_date']).dt.days.clip(lower=1)
            df_copy['velocity'] = df_copy['views'] / df_copy['days_live']
            return df_copy['velocity'].round(2)
        else:
            # Fallback to individual calculation
            return df.apply(lambda row: self._calculate_growth_velocity(
                row['video_id'], row['views'], row['publish_date']
            ), axis=1)
    
    def _vectorized_calculate_virality_scores(self, df: pd.DataFrame) -> pd.Series:
        """Calculate virality scores for all videos using vectorized operations."""
        virality_scores = []
        
        for _, row in df.iterrows():
            score = self._calculate_virality_score(
                row['video_id'],
                row['views'],
                row['engagement_rate'],
                row['growth_velocity'],
                row['retention_rate'],
                row['days_live']
            )
            virality_scores.append(score)
        
        return pd.Series(virality_scores, index=df.index)
    
    async def _batch_ml_predictions_async(self, video_features: List[Tuple[str, Dict]]) -> Dict[str, MLPrediction]:
        """Async batch ML predictions with concurrency control."""
        if not self.enable_async_ml:
            # Fallback to synchronous batch predictions
            return self._load_ml_predictions([vid for vid, _ in video_features])
        
        try:
            # Split into smaller chunks for concurrent processing
            chunk_size = min(100, len(video_features))
            chunks = [video_features[i:i + chunk_size] for i in range(0, len(video_features), chunk_size)]
            
            # Process chunks concurrently
            semaphore = asyncio.Semaphore(self.max_concurrent_batches)
            
            async def process_chunk(chunk):
                async with semaphore:
                    tasks = [self.ml_predictor.predict_async(vid, features) for vid, features in chunk]
                    results = await asyncio.gather(*tasks)
                    return list(zip([vid for vid, _ in chunk], results))
            
            # Execute all chunks concurrently
            chunk_results = await asyncio.gather(*[process_chunk(chunk) for chunk in chunks])
            
            # Combine results
            predictions = {}
            for chunk_result in chunk_results:
                for video_id, prediction in chunk_result:
                    predictions[video_id] = prediction
            
            return predictions
            
        except Exception as e:
            print(f"[FactoryMetrics] Error in async batch ML predictions: {e}")
            # Fallback to synchronous
            return self._load_ml_predictions([vid for vid, _ in video_features])
    
    def _vectorized_calculate_engagement_rates(self, df: pd.DataFrame) -> pd.Series:
        """Calculate engagement rates for all videos using vectorized operations."""
        if self.scalability_config['vectorized_ops'] and len(df) > 100:
            # Vectorized calculation for large datasets
            try:
                # Use numpy vectorization instead of pandas apply()
                views = df['views'].values
                likes = df['likes'].values
                comments = df['comments'].values
                shares = df['shares'].values
                platforms = df['platform'].values
                
                # Vectorized engagement calculation
                engagement_rates = np.zeros(len(df))
                
                for i, platform in enumerate(platforms):
                    weights = self.platform_weights.get(str(platform).lower(), 
                                                   self.platform_weights['youtube'])
                    weighted_engagement = (
                        likes[i] * weights['likes'] +
                        comments[i] * weights['comments'] +
                        shares[i] * weights['shares']
                    )
                    engagement_rates[i] = weighted_engagement / views[i] if views[i] > 0 else 0.0
                
                return pd.Series(engagement_rates.round(4), index=df.index)
            except Exception as e:
                print(f"[FactoryMetrics] Error in vectorized engagement calculation: {e}")
                # Fallback to pandas apply
                return df.apply(lambda row: self._calculate_engagement_rate(
                    row['views'], row['likes'], row['comments'], row['shares'], row['platform']
                ), axis=1)
        else:
            # Fallback to row-wise calculation for small datasets
            return df.apply(lambda row: self._calculate_engagement_rate(
                row['views'], row['likes'], row['comments'], row['shares'], row['platform']
            ), axis=1)
    
    def _vectorized_calculate_growth_velocities(self, df: pd.DataFrame) -> pd.Series:
        """Calculate growth velocities for all videos using vectorized operations."""
        if self.scalability_config['vectorized_ops'] and len(df) > 100:
            try:
                # Vectorized calculation using numpy
                now = datetime.now()
                df_copy = df.copy()
                
                # Vectorized days calculation
                if 'publish_date' in df_copy.columns:
                    days_live = (now - df_copy['publish_date']).dt.days.clip(lower=1)
                    velocities = df_copy['views'].values / days_live.astype(float)
                    return pd.Series(velocities.round(2), index=df.index)
                else:
                    # Fallback if publish_date missing
                    return pd.Series([0.0] * len(df), index=df.index)
            except Exception as e:
                print(f"[FactoryMetrics] Error in vectorized velocity calculation: {e}")
                # Fallback to pandas apply
                return df.apply(lambda row: self._calculate_growth_velocity(
                    row['video_id'], row['views'], row['publish_date']
                ), axis=1)
        else:
            # Fallback to individual calculation
            return df.apply(lambda row: self._calculate_growth_velocity(
                row['video_id'], row['views'], row['publish_date']
            ), axis=1)
    
    def _vectorized_calculate_virality_scores(self, df: pd.DataFrame) -> pd.Series:
        """Calculate virality scores for all videos using vectorized operations."""
        try:
            if self.scalability_config['vectorized_ops'] and len(df) > 100:
                # Vectorized virality score calculation
                virality_scores = []
                
                for _, row in df.iterrows():
                    score = self._calculate_virality_score(
                        row['video_id'],
                        row['views'],
                        row['engagement_rate'],
                        row['growth_velocity'],
                        row['retention_rate'],
                        row['days_live']
                    )
                    virality_scores.append(score)
                
                return pd.Series(virality_scores, index=df.index)
            else:
                # Fallback to individual calculation
                virality_scores = []
                for _, row in df.iterrows():
                    score = self._calculate_virality_score(
                        row['video_id'],
                        row['views'],
                        row['engagement_rate'],
                        row['growth_velocity'],
                        row['retention_rate'],
                        row['days_live']
                    )
                    virality_scores.append(score)
                
                return pd.Series(virality_scores, index=df.index)
        except Exception as e:
            print(f"[FactoryMetrics] Error in vectorized virality calculation: {e}")
            return pd.Series([50.0] * len(df), index=df.index)
    
    def _high_performance_batch_compute_metrics(self, video_ids: Optional[List[str]] = None) -> BatchMetricsResult:
        """
        High-performance batch metrics computation optimized for 50k-100k videos/day.
        
        Uses vectorized operations, streaming I/O, and columnar optimizations.
        """
        start_time = datetime.now()
        
        try:
            # Get videos to process
            if video_ids is None:
                video_ids = self.video_stats['video_id'].tolist()
            
            # Filter valid videos
            valid_videos = [vid for vid in video_ids if vid in self.video_stats['video_id'].values]
            
            if not valid_videos:
                return BatchMetricsResult(
                    video_kpis=[],
                    processing_time_ms=0.0,
                    videos_processed=0,
                    throughput_per_second=0.0,
                    batch_size=len(video_ids),
                    ml_prediction_time_ms=0.0,
                    long_tail_time_ms=0.0
                )
            
            # Process in optimized chunks
            all_kpis = []
            total_ml_time = 0.0
            total_long_tail_time = 0.0
            chunk_size = self.scalability_config['chunk_size']
            
            # Process in chunks for memory efficiency
            for i in range(0, len(valid_videos), chunk_size):
                chunk_videos = valid_videos[i:i + chunk_size]
                
                # Pre-batch hook for distributed processing
                if self.distributed_hooks['pre_batch_hook']:
                    self.distributed_hooks['pre_batch_hook'](chunk_videos)
                
                # Get chunk data with columnar optimization
                chunk_df = self.video_stats[self.video_stats['video_id'].isin(chunk_videos)].copy()
                
                # Vectorized calculations
                chunk_start = datetime.now()
                
                # Calculate all metrics vectorized
                chunk_df['engagement_rate'] = self._vectorized_calculate_engagement_rates(chunk_df)
                chunk_df['growth_velocity'] = self._vectorized_calculate_growth_velocities(chunk_df)
                chunk_df['days_live'] = (datetime.now() - chunk_df['publish_date']).dt.days.clip(lower=1)
                
                # High-performance ML predictions
                ml_start = datetime.now()
                ml_predictions = self._batch_ml_predictions_optimized(chunk_videos, chunk_df)
                ml_time = (datetime.now() - ml_start).total_seconds() * 1000
                total_ml_time += ml_time
                
                # High-performance long-tail calculations
                long_tail_start = datetime.now()
                long_tail_scores = self._batch_long_tail_optimized(chunk_videos)
                total_long_tail_time += (datetime.now() - long_tail_start).total_seconds() * 1000
                
                # Vectorized virality scores
                chunk_df['virality_score'] = self._vectorized_calculate_virality_scores(chunk_df)
                
                # Create VideoKPIs objects efficiently
                chunk_kpis = self._create_video_kpis_vectorized(chunk_df, ml_predictions, long_tail_scores)
                all_kpis.extend(chunk_kpis)
                
                # Post-batch hook for distributed processing
                if self.distributed_hooks['post_batch_hook']:
                    self.distributed_hooks['post_batch_hook'](chunk_videos, chunk_kpis)
                
                # Progress callback
                if self.distributed_hooks['progress_callback']:
                    self.distributed_hooks['progress_callback'](i, len(valid_videos), len(chunk_videos))
                
                # Checkpoint for large datasets
                checkpoint_interval = self.scalability_config['checkpoint_interval']
                if (i + chunk_size) % checkpoint_interval == 0:
                    if self.distributed_hooks['checkpoint_handler']:
                        self.distributed_hooks['checkpoint_handler'](all_kpis)
            
            # Calculate performance metrics
            processing_time = (datetime.now() - start_time).total_seconds() * 1000
            throughput = len(all_kpis) / (processing_time / 1000) if processing_time > 0 else 0
            
            # Update processing stats
            self.processing_stats['total_processed'] += len(all_kpis)
            self.processing_stats['processing_rate'] = throughput
            self.processing_stats['memory_usage_mb'] = self._estimate_memory_usage()
            
            return BatchMetricsResult(
                video_kpis=all_kpis,
                processing_time_ms=processing_time,
                videos_processed=len(all_kpis),
                throughput_per_second=throughput,
                batch_size=len(video_ids),
                ml_prediction_time_ms=total_ml_time,
                long_tail_time_ms=total_long_tail_time
            )
            
        except Exception as e:
            print(f"[FactoryMetrics] Error in high-performance batch compute: {e}")
            # Error handler hook
            if self.distributed_hooks['error_handler']:
                self.distributed_hooks['error_handler'](e, video_ids)
            
            return BatchMetricsResult(
                video_kpis=[],
                processing_time_ms=0.0,
                videos_processed=0,
                throughput_per_second=0.0,
                batch_size=len(video_ids) if video_ids else 0,
                ml_prediction_time_ms=0.0,
                long_tail_time_ms=0.0
            )
    
    def _batch_ml_predictions_optimized(self, video_ids: List[str], df: pd.DataFrame) -> Dict[str, MLPrediction]:
        """Optimized batch ML predictions with caching and vectorization."""
        try:
            predictions = {}
            uncached_videos = []
            
            # Check cache first
            for video_id in video_ids:
                if video_id in self._ml_predictions_cache:
                    predictions[video_id] = self._ml_predictions_cache[video_id]
                else:
                    uncached_videos.append(video_id)
            
            # Batch predict only uncached videos
            if uncached_videos:
                # Prepare features efficiently
                video_features = []
                for video_id in uncached_videos:
                    if video_id in df['video_id'].values:
                        row = df[df['video_id'] == video_id].iloc[0]
                        features = {
                            'views': int(row['views']),
                            'likes': int(row['likes']),
                            'comments': int(row['comments']),
                            'shares': int(row['shares']),
                            'retention_rate': float(row.get('retention_rate', 0.5)),
                            'engagement_rate': float(row['engagement_rate']),
                            'growth_velocity': float(row['growth_velocity']),
                            'days_live': int(row['days_live']),
                            'platform': str(row.get('platform', 'youtube')),
                            'publish_date': row['publish_date'].isoformat()
                        }
                        video_features.append((video_id, features))
                
                # Batch prediction
                batch_predictions = self.ml_predictor.predict_batch(video_features)
                
                # Update cache
                for (video_id, _), prediction in zip(video_features, batch_predictions):
                    predictions[video_id] = prediction
                    self._ml_predictions_cache[video_id] = prediction
            
            return predictions
            
        except Exception as e:
            print(f"[FactoryMetrics] Error in optimized ML predictions: {e}")
            return {}
    
    def _batch_long_tail_optimized(self, video_ids: List[str]) -> Dict[str, float]:
        """Optimized batch long-tail score calculation."""
        try:
            long_tail_scores = {}
            
            if self.long_tail_tracker:
                for video_id in video_ids:
                    try:
                        score = self.long_tail_tracker.compute_long_tail_score(video_id)
                        long_tail_scores[video_id] = score
                    except Exception as e:
                        print(f"[FactoryMetrics] Error getting long-tail score for {video_id}: {e}")
                        long_tail_scores[video_id] = 0.0
            else:
                # Fallback heuristic
                for video_id in video_ids:
                    if video_id in self.video_stats['video_id'].values:
                        video = self.video_stats[self.video_stats['video_id'] == video_id].iloc[0]
                        days_live = (datetime.now() - pd.to_datetime(video['publish_date'])).days
                        growth_velocity = self._calculate_growth_velocity(
                            video_id, int(video['views']), pd.to_datetime(video['publish_date'])
                        )
                        long_tail_scores[video_id] = 15.0 if (days_live > 30 and growth_velocity > 1000) else 0.0
            
            return long_tail_scores
            
        except Exception as e:
            print(f"[FactoryMetrics] Error in optimized long-tail calculation: {e}")
            return {}
    
    def _create_video_kpis_vectorized(self, df: pd.DataFrame, ml_predictions: Dict[str, MLPrediction], long_tail_scores: Dict[str, float]) -> List[VideoKPIs]:
        """Create VideoKPIs objects using vectorized operations."""
        try:
            video_kpis = []
            
            for _, row in df.iterrows():
                video_id = row['video_id']
                prediction = ml_predictions.get(video_id)
                long_tail_score = long_tail_scores.get(video_id, 0.0)
                
                # Handle ML prediction
                if isinstance(prediction, MLPrediction):
                    projected_final_views = prediction.predicted_views
                elif prediction:
                    projected_final_views = prediction
                else:
                    projected_final_views = int(row['views'] + (row['growth_velocity'] * 30))
                
                # Calculate intervention needs
                meets_baseline = row['views'] >= self.baseline_views
                needs_intervention = (
                    (row['days_live'] >= self.intervention_threshold_days and not meets_baseline) or
                    (projected_final_views < self.baseline_views * 0.8) or
                    (row['engagement_rate'] < self.target_engagement_rate * 0.5) or
                    (row['retention_rate'] < self.min_retention_rate * 0.7)
                )
                
                kpi = VideoKPIs(
                    video_id=video_id,
                    timestamp=row['timestamp'],
                    total_views=int(row['views']),
                    likes=int(row['likes']),
                    comments=int(row['comments']),
                    shares=int(row['shares']),
                    watch_time_hours=float(row.get('watch_time_hours', row['views'] * 0.05)),
                    avg_retention_rate=float(row['retention_rate']),
                    engagement_rate=float(row['engagement_rate']),
                    growth_velocity=float(row['growth_velocity']),
                    virality_score=float(row['virality_score']),
                    days_since_publish=int(row['days_live']),
                    meets_baseline=meets_baseline,
                    needs_intervention=needs_intervention,
                    projected_final_views=projected_final_views
                )
                
                video_kpis.append(kpi)
            
            return video_kpis
            
        except Exception as e:
            print(f"[FactoryMetrics] Error creating vectorized VideoKPIs: {e}")
            return []
    
    def _estimate_memory_usage(self) -> float:
        """Estimate current memory usage in MB."""
        try:
            import psutil
            process = psutil.Process()
            return process.memory_info().rss / 1024 / 1024
        except ImportError:
            return 0.0
        except Exception:
            return 0.0
    
    def batch_compute_metrics(self, video_ids: Optional[List[str]] = None) -> BatchMetricsResult:
        """
        Compute metrics for multiple videos in batches for high-throughput processing.
        
        Uses high-performance optimizations for 50k-100k videos/day.
        """
        try:
            # Use high-performance batch processing for large datasets
            if self.scalability_config['batch_optimization'] and (video_ids is None or len(video_ids) > 1000):
                return self._high_performance_batch_compute_metrics(video_ids)
            else:
                # Fallback to traditional batch processing for smaller datasets
                return self._traditional_batch_compute_metrics(video_ids)
        except Exception as e:
            print(f"[FactoryMetrics] Error in batch compute metrics: {e}")
            return BatchMetricsResult(
                video_kpis=[],
                processing_time_ms=0.0,
                videos_processed=0,
                throughput_per_second=0.0,
                batch_size=len(video_ids) if video_ids else 0,
                ml_prediction_time_ms=0.0,
                long_tail_time_ms=0.0
            )
    
    def _traditional_batch_compute_metrics(self, video_ids: Optional[List[str]] = None) -> BatchMetricsResult:
        """
        Compute metrics for multiple videos in batches for high-throughput processing.
        
        Args:
            video_ids: List of video IDs to process. If None, process all videos.
        
        Returns:
            BatchMetricsResult: Results with performance metrics
        """
        start_time = datetime.now()
        
        try:
            # Get videos to process
            if video_ids is None:
                video_ids = self.video_stats['video_id'].tolist()
            
            # Filter valid videos
            valid_videos = [vid for vid in video_ids if vid in self.video_stats['video_id'].values]
            
            if not valid_videos:
                return BatchMetricsResult(
                    video_kpis=[],
                    processing_time_ms=0.0,
                    videos_processed=0,
                    throughput_per_second=0.0,
                    batch_size=len(video_ids),
                    ml_prediction_time_ms=0.0,
                    long_tail_time_ms=0.0
                )
            
            # Process in batches
            all_kpis = []
            total_ml_time = 0.0
            total_long_tail_time = 0.0
            
            for i in range(0, len(valid_videos), self.batch_size):
                batch_videos = valid_videos[i:i + self.batch_size]
                batch_df = self.video_stats[self.video_stats['video_id'].isin(batch_videos)].copy()
                
                # Vectorized calculations
                batch_start = datetime.now()
                
                # Calculate engagement rates
                batch_df['engagement_rate'] = self._vectorized_calculate_engagement_rates(batch_df)
                
                # Calculate growth velocities
                batch_df['growth_velocity'] = self._vectorized_calculate_growth_velocities(batch_df)
                
                # Calculate days live
                now = datetime.now()
                batch_df['days_live'] = (now - batch_df['publish_date']).dt.days.clip(lower=1)
                
                # Batch ML predictions
                ml_start = datetime.now()
                video_features = []
                for _, row in batch_df.iterrows():
                    features = {
                        'views': int(row['views']),
                        'likes': int(row['likes']),
                        'comments': int(row['comments']),
                        'shares': int(row['shares']),
                        'retention_rate': float(row.get('retention_rate', 0.5)),
                        'engagement_rate': float(row['engagement_rate']),
                        'growth_velocity': float(row['growth_velocity']),
                        'days_live': int(row['days_live']),
                        'platform': str(row.get('platform', 'youtube')),
                        'publish_date': row['publish_date'].isoformat()
                    }
                    video_features.append((row['video_id'], features))
                
                batch_predictions = self.ml_predictor.predict_batch(video_features)
                ml_time = (datetime.now() - ml_start).total_seconds() * 1000
                total_ml_time += ml_time
                
                # Update cache
                for (video_id, _), prediction in zip(video_features, batch_predictions):
                    self._ml_predictions_cache[video_id] = prediction
                
                # Batch long-tail calculations
                long_tail_start = datetime.now()
                long_tail_scores = {}
                if self.long_tail_tracker:
                    for video_id in batch_videos:
                        try:
                            score = self.long_tail_tracker.compute_long_tail_score(video_id)
                            long_tail_scores[video_id] = score
                        except Exception as e:
                            print(f"[FactoryMetrics] Error getting long-tail score for {video_id}: {e}")
                            long_tail_scores[video_id] = 0.0
                
                long_tail_time = (datetime.now() - long_tail_start).total_seconds() * 1000
                total_long_tail_time += long_tail_time
                
                # Calculate virality scores
                batch_df['virality_score'] = self._vectorized_calculate_virality_scores(batch_df)
                
                # Create VideoKPIs objects
                for _, row in batch_df.iterrows():
                    prediction = self._ml_predictions_cache.get(row['video_id'])
                    projected_views = prediction.predicted_views if isinstance(prediction, MLPrediction) else int(row['views'] + row['growth_velocity'] * 30)
                    
                    kpi = VideoKPIs(
                        video_id=row['video_id'],
                        timestamp=pd.to_datetime(row['timestamp']),
                        total_views=int(row['views']),
                        likes=int(row['likes']),
                        comments=int(row['comments']),
                        shares=int(row['shares']),
                        watch_time_hours=float(row.get('watch_time_hours', row['views'] * 0.05)),
                        avg_retention_rate=float(row.get('retention_rate', 0.5)),
                        engagement_rate=float(row['engagement_rate']),
                        growth_velocity=float(row['growth_velocity']),
                        virality_score=float(row['virality_score']),
                        days_since_publish=int(row['days_live']),
                        meets_baseline=row['views'] >= self.baseline_views,
                        needs_intervention=(
                            (row['days_live'] >= self.intervention_threshold_days and row['views'] < self.baseline_views) or
                            (projected_views < self.baseline_views * 0.8) or
                            (row['engagement_rate'] < self.target_engagement_rate * 0.5) or
                            (row.get('retention_rate', 0.5) < self.min_retention_rate * 0.7)
                        ),
                        projected_final_views=projected_views
                    )
                    all_kpis.append(kpi)
            
            # Calculate performance metrics
            end_time = datetime.now()
            processing_time_ms = (end_time - start_time).total_seconds() * 1000
            throughput_per_second = len(all_kpis) / (processing_time_ms / 1000) if processing_time_ms > 0 else 0
            
            # Update batch metrics
            self.batch_metrics['total_batches_processed'] += 1
            self.batch_metrics['total_videos_processed'] += len(all_kpis)
            self.batch_metrics['avg_processing_time_ms'] = (
                (self.batch_metrics['avg_processing_time_ms'] * (self.batch_metrics['total_batches_processed'] - 1) + processing_time_ms) /
                self.batch_metrics['total_batches_processed']
            )
            self.batch_metrics['peak_throughput_per_second'] = max(
                self.batch_metrics['peak_throughput_per_second'], throughput_per_second
            )
            
            return BatchMetricsResult(
                video_kpis=all_kpis,
                processing_time_ms=processing_time_ms,
                videos_processed=len(all_kpis),
                throughput_per_second=throughput_per_second,
                batch_size=len(video_ids),
                ml_prediction_time_ms=total_ml_time,
                long_tail_time_ms=total_long_tail_time
            )
            
        except Exception as e:
            print(f"[FactoryMetrics] Error in batch compute metrics: {e}")
            return BatchMetricsResult(
                video_kpis=[],
                processing_time_ms=0.0,
                videos_processed=0,
                throughput_per_second=0.0,
                batch_size=len(video_ids) if video_ids else 0,
                ml_prediction_time_ms=0.0,
                long_tail_time_ms=0.0
            )
    
    async def async_batch_compute_metrics(self, video_ids: Optional[List[str]] = None) -> BatchMetricsResult:
        """
        Async version of batch metrics computation with concurrent ML predictions.
        
        Args:
            video_ids: List of video IDs to process. If None, process all videos.
        
        Returns:
            BatchMetricsResult: Results with performance metrics
        """
        start_time = datetime.now()
        
        try:
            # Get videos to process
            if video_ids is None:
                video_ids = self.video_stats['video_id'].tolist()
            
            # Filter valid videos
            valid_videos = [vid for vid in video_ids if vid in self.video_stats['video_id'].values]
            
            if not valid_videos:
                return BatchMetricsResult(
                    video_kpis=[],
                    processing_time_ms=0.0,
                    videos_processed=0,
                    throughput_per_second=0.0,
                    batch_size=len(video_ids),
                    ml_prediction_time_ms=0.0,
                    long_tail_time_ms=0.0
                )
            
            # Process in batches with async ML predictions
            all_kpis = []
            total_ml_time = 0.0
            total_long_tail_time = 0.0
            
            for i in range(0, len(valid_videos), self.batch_size):
                batch_videos = valid_videos[i:i + self.batch_size]
                batch_df = self.video_stats[self.video_stats['video_id'].isin(batch_videos)].copy()
                
                # Vectorized calculations (same as sync version)
                batch_df['engagement_rate'] = self._vectorized_calculate_engagement_rates(batch_df)
                batch_df['growth_velocity'] = self._vectorized_calculate_growth_velocities(batch_df)
                now = datetime.now()
                batch_df['days_live'] = (now - batch_df['publish_date']).dt.days.clip(lower=1)
                
                # Async ML predictions
                ml_start = datetime.now()
                video_features = []
                for _, row in batch_df.iterrows():
                    features = {
                        'views': int(row['views']),
                        'likes': int(row['likes']),
                        'comments': int(row['comments']),
                        'shares': int(row['shares']),
                        'retention_rate': float(row.get('retention_rate', 0.5)),
                        'engagement_rate': float(row['engagement_rate']),
                        'growth_velocity': float(row['growth_velocity']),
                        'days_live': int(row['days_live']),
                        'platform': str(row.get('platform', 'youtube')),
                        'publish_date': row['publish_date'].isoformat()
                    }
                    video_features.append((row['video_id'], features))
                
                batch_predictions = await self._batch_ml_predictions_async(video_features)
                ml_time = (datetime.now() - ml_start).total_seconds() * 1000
                total_ml_time += ml_time
                
                # Update cache
                self._ml_predictions_cache.update(batch_predictions)
                
                # Continue with rest of processing (same as sync version)
                long_tail_start = datetime.now()
                long_tail_scores = {}
                if self.long_tail_tracker:
                    for video_id in batch_videos:
                        try:
                            score = self.long_tail_tracker.compute_long_tail_score(video_id)
                            long_tail_scores[video_id] = score
                        except Exception as e:
                            print(f"[FactoryMetrics] Error getting long-tail score for {video_id}: {e}")
                            long_tail_scores[video_id] = 0.0
                
                long_tail_time = (datetime.now() - long_tail_start).total_seconds() * 1000
                total_long_tail_time += long_tail_time
                
                batch_df['virality_score'] = self._vectorized_calculate_virality_scores(batch_df)
                
                # Create VideoKPIs objects
                for _, row in batch_df.iterrows():
                    prediction = self._ml_predictions_cache.get(row['video_id'])
                    projected_views = prediction.predicted_views if isinstance(prediction, MLPrediction) else int(row['views'] + row['growth_velocity'] * 30)
                    
                    kpi = VideoKPIs(
                        video_id=row['video_id'],
                        timestamp=pd.to_datetime(row['timestamp']),
                        total_views=int(row['views']),
                        likes=int(row['likes']),
                        comments=int(row['comments']),
                        shares=int(row['shares']),
                        watch_time_hours=float(row.get('watch_time_hours', row['views'] * 0.05)),
                        avg_retention_rate=float(row.get('retention_rate', 0.5)),
                        engagement_rate=float(row['engagement_rate']),
                        growth_velocity=float(row['growth_velocity']),
                        virality_score=float(row['virality_score']),
                        days_since_publish=int(row['days_live']),
                        meets_baseline=row['views'] >= self.baseline_views,
                        needs_intervention=(
                            (row['days_live'] >= self.intervention_threshold_days and row['views'] < self.baseline_views) or
                            (projected_views < self.baseline_views * 0.8) or
                            (row['engagement_rate'] < self.target_engagement_rate * 0.5) or
                            (row.get('retention_rate', 0.5) < self.min_retention_rate * 0.7)
                        ),
                        projected_final_views=projected_views
                    )
                    all_kpis.append(kpi)
            
            # Calculate performance metrics
            end_time = datetime.now()
            processing_time_ms = (end_time - start_time).total_seconds() * 1000
            throughput_per_second = len(all_kpis) / (processing_time_ms / 1000) if processing_time_ms > 0 else 0
            
            return BatchMetricsResult(
                video_kpis=all_kpis,
                processing_time_ms=processing_time_ms,
                videos_processed=len(all_kpis),
                throughput_per_second=throughput_per_second,
                batch_size=len(video_ids),
                ml_prediction_time_ms=total_ml_time,
                long_tail_time_ms=total_long_tail_time
            )
            
        except Exception as e:
            print(f"[FactoryMetrics] Error in async batch compute metrics: {e}")
            return BatchMetricsResult(
                video_kpis=[],
                processing_time_ms=0.0,
                videos_processed=0,
                throughput_per_second=0.0,
                batch_size=len(video_ids) if video_ids else 0,
                ml_prediction_time_ms=0.0,
                long_tail_time_ms=0.0
            )
    
    def get_batch_performance_metrics(self) -> Dict:
        """Get performance metrics for batch processing."""
        return {
            'total_batches_processed': self.batch_metrics['total_batches_processed'],
            'total_videos_processed': self.batch_metrics['total_videos_processed'],
            'avg_processing_time_ms': round(self.batch_metrics['avg_processing_time_ms'], 2),
            'peak_throughput_per_second': round(self.batch_metrics['peak_throughput_per_second'], 2),
            'batch_size': self.batch_size,
            'enable_async_ml': self.enable_async_ml,
            'use_vectorized_ops': self.use_vectorized_ops,
            'max_concurrent_batches': self.max_concurrent_batches
        }
    
    def _normalize_state_vector(self, kpi: VideoKPIs, prediction: Optional[MLPrediction] = None) -> RLStateVector:
        """Create normalized 8-dimensional state vector for RL policy input."""
        try:
            # Normalize virality score (0-100 -> 0-1)
            virality_normalized = min(1.0, kpi.virality_score / 100.0)
            
            # Normalize baseline gap ratio (0-1, where 1 = at baseline)
            baseline_gap_ratio = min(1.0, kpi.total_views / self.baseline_views)
            
            # Normalize growth velocity (0-1, based on expected daily views)
            expected_daily = self.baseline_views / 30
            velocity_normalized = min(1.0, kpi.growth_velocity / (expected_daily * 2))
            
            # Normalize engagement rate (0-1, based on target)
            engagement_normalized = min(1.0, kpi.engagement_rate / (self.target_engagement_rate * 2))
            
            # Normalize retention rate (0-1, based on minimum)
            retention_normalized = min(1.0, kpi.avg_retention_rate / self.min_retention_rate)
            
            # Normalize days live (0-1, log scale for 1-90 days)
            days_normalized = min(1.0, np.log1p(kpi.days_since_publish) / np.log1p(90))
            
            # Normalize long-tail score (0-1)
            long_tail_score = 0.0
            if self.long_tail_tracker:
                try:
                    long_tail_score = self.long_tail_tracker.compute_long_tail_score(kpi.video_id)
                except Exception:
                    long_tail_score = 0.0
            long_tail_normalized = min(1.0, long_tail_score)
            
            # Normalize confidence score (0-1, from ML prediction confidence)
            confidence_normalized = 0.5  # Default confidence
            if prediction and isinstance(prediction, MLPrediction):
                confidence_width = prediction.confidence_high - prediction.confidence_low
                confidence_normalized = max(0.0, 1.0 - (confidence_width / prediction.predicted_views))
            
            return RLStateVector(
                virality_score_normalized=virality_normalized,
                baseline_gap_ratio=baseline_gap_ratio,
                growth_velocity_normalized=velocity_normalized,
                engagement_rate_normalized=engagement_normalized,
                retention_rate_normalized=retention_normalized,
                days_live_normalized=days_normalized,
                long_tail_score_normalized=long_tail_normalized,
                confidence_score_normalized=confidence_normalized
            )
            
        except Exception as e:
            print(f"[FactoryMetrics] Error normalizing state vector for {kpi.video_id}: {e}")
            # Return default state vector
            return RLStateVector(
                virality_score_normalized=0.5,
                baseline_gap_ratio=0.5,
                growth_velocity_normalized=0.5,
                engagement_rate_normalized=0.5,
                retention_rate_normalized=0.5,
                days_live_normalized=0.5,
                long_tail_score_normalized=0.5,
                confidence_score_normalized=0.5
            )
    
    def _calculate_reward_components(self, kpi: VideoKPIs, state: RLStateVector) -> Dict[str, float]:
        """Calculate individual reward components for RL learning."""
        components = {}
        
        # Virality reward (0-1 scaled to -0.3 to 0.3)
        virality_reward = (state.virality_score_normalized - 0.5) * 0.6
        components['virality'] = virality_reward
        
        # Baseline compliance reward (-0.4 to 0.4)
        baseline_reward = (state.baseline_gap_ratio - 0.5) * 0.8
        components['baseline_compliance'] = baseline_reward
        
        # Growth velocity reward (-0.2 to 0.2)
        velocity_reward = (state.growth_velocity_normalized - 0.5) * 0.4
        components['growth_velocity'] = velocity_reward
        
        # Engagement reward (-0.1 to 0.1)
        engagement_reward = (state.engagement_rate_normalized - 0.5) * 0.2
        components['engagement'] = engagement_reward
        
        # Retention reward (-0.1 to 0.1)
        retention_reward = (state.retention_rate_normalized - 0.5) * 0.2
        components['retention'] = retention_reward
        
        # Long-tail potential reward (-0.05 to 0.05)
        long_tail_reward = (state.long_tail_score_normalized - 0.5) * 0.1
        components['long_tail'] = long_tail_reward
        
        # Time penalty for old videos (-0.05 to 0)
        time_penalty = -state.days_live_normalized * 0.05
        components['time_penalty'] = time_penalty
        
        return components
    
    def compute_rl_reward_signal(self, video_id: str, action_taken: Optional[str] = None) -> RLRewardSignal:
        """
        Compute complete RL reward signal for reward shaping.
        
        reward = f(virality_score, baseline_gap, velocity)
        
        Args:
            video_id: Video ID to compute reward for
            action_taken: Optional action that was taken
        
        Returns:
            RLRewardSignal: Complete reward signal with state and feedback
        """
        try:
            # Get video KPIs
            kpi = self.compute_per_video_metrics(video_id)
            if not kpi:
                # Return neutral reward for missing video
                return RLRewardSignal(
                    video_id=video_id,
                    reward=0.0,
                    reward_components={'missing_video': 0.0},
                    state_vector=RLStateVector(
                        virality_score_normalized=0.0,
                        baseline_gap_ratio=0.0,
                        growth_velocity_normalized=0.0,
                        engagement_rate_normalized=0.0,
                        retention_rate_normalized=0.0,
                        days_live_normalized=0.0,
                        long_tail_score_normalized=0.0,
                        confidence_score_normalized=0.0
                    ),
                    action_state=None,
                    feedback_signal=None,
                    confidence_interval=(0.0, 0.0),
                    model_version='unknown',
                    timestamp=datetime.now()
                )
            
            # Get ML prediction for confidence
            prediction = self._ml_predictions_cache.get(video_id)
            
            # Create state vector
            state_vector = self._normalize_state_vector(kpi, prediction)
            
            # Calculate reward components
            reward_components = self._calculate_reward_components(kpi, state_vector)
            
            # Compute primary reward (sum of components, clamped to -1 to 1)
            primary_reward = sum(reward_components.values())
            primary_reward = max(-1.0, min(1.0, primary_reward))
            
            # Create feedback signal if action was taken
            feedback_signal = None
            if action_taken:
                # Calculate action effectiveness based on state improvement
                effectiveness = 0.0
                if action_taken == 'boost' and state_vector.virality_score_normalized > 0.7:
                    effectiveness = 0.8
                elif action_taken == 'repost' and state_vector.baseline_gap_ratio < 0.5:
                    effectiveness = 0.6
                elif action_taken == 'optimize' and state_vector.engagement_rate_normalized > 0.6:
                    effectiveness = 0.7
                
                feedback_signal = RLFeedbackSignal(
                    action_effectiveness=effectiveness,
                    cost_benefit_ratio=0.5,  # Simplified cost-benefit
                    urgency_score=max(0.0, 1.0 - state_vector.baseline_gap_ratio),
                    intervention_type=action_taken,
                    expected_improvement=primary_reward * 100000  # Expected view improvement
                )
            
            # Calculate confidence interval for reward
            confidence_width = 0.1  # Default confidence
            if prediction and isinstance(prediction, MLPrediction):
                # Use ML prediction confidence to estimate reward uncertainty
                confidence_width = (prediction.confidence_high - prediction.confidence_low) / prediction.predicted_views * 0.2
            confidence_interval = (
                max(-1.0, primary_reward - confidence_width),
                min(1.0, primary_reward + confidence_width)
            )
            
            return RLRewardSignal(
                video_id=video_id,
                reward=primary_reward,
                reward_components=reward_components,
                state_vector=state_vector,
                action_state=None,  # Could be extended for action-state mapping
                feedback_signal=feedback_signal,
                confidence_interval=confidence_interval,
                model_version=prediction.model_version if prediction else 'unknown',
                timestamp=datetime.now()
            )
            
        except Exception as e:
            print(f"[FactoryMetrics] Error computing RL reward signal for {video_id}: {e}")
            # Return neutral reward on error
            return RLRewardSignal(
                video_id=video_id,
                reward=0.0,
                reward_components={'error': 0.0},
                state_vector=RLStateVector(
                    virality_score_normalized=0.5,
                    baseline_gap_ratio=0.5,
                    growth_velocity_normalized=0.5,
                    engagement_rate_normalized=0.5,
                    retention_rate_normalized=0.5,
                    days_live_normalized=0.5,
                    long_tail_score_normalized=0.5,
                    confidence_score_normalized=0.5
                ),
                action_state=None,
                feedback_signal=None,
                confidence_interval=(0.0, 0.0),
                model_version='error',
                timestamp=datetime.now()
            )
    
    def batch_compute_rl_rewards(self, video_ids: Optional[List[str]] = None) -> List[RLRewardSignal]:
        """
        Compute RL reward signals for multiple videos in batch.
        
        Args:
            video_ids: List of video IDs to compute rewards for. If None, process all videos.
        
        Returns:
            List[RLRewardSignal]: Batch of reward signals
        """
        try:
            if video_ids is None:
                video_ids = self.video_stats['video_id'].tolist()
            
            reward_signals = []
            
            # Process in batches for efficiency
            for i in range(0, len(video_ids), self.batch_size):
                batch_video_ids = video_ids[i:i + self.batch_size]
                
                for video_id in batch_video_ids:
                    reward_signal = self.compute_rl_reward_signal(video_id)
                    reward_signals.append(reward_signal)
            
            return reward_signals
            
        except Exception as e:
            print(f"[FactoryMetrics] Error in batch RL reward computation: {e}")
            return []
    
    def get_rl_policy_feedback(self, video_ids: List[str], actions_taken: List[str]) -> List[RLFeedbackSignal]:
        """
        Get policy feedback signals for RL learning.
        
        Args:
            video_ids: List of video IDs
            actions_taken: List of actions taken (parallel to video_ids)
        
        Returns:
            List[RLFeedbackSignal]: Policy feedback signals
        """
        feedback_signals = []
        
        for video_id, action in zip(video_ids, actions_taken):
            try:
                kpi = self.compute_per_video_metrics(video_id)
                if not kpi:
                    continue
                
                prediction = self._ml_predictions_cache.get(video_id)
                state = self._normalize_state_vector(kpi, prediction)
                
                # Calculate action effectiveness
                effectiveness = 0.0
                if action == 'boost' and state.virality_score_normalized > 0.7:
                    effectiveness = 0.8
                elif action == 'repost' and state.baseline_gap_ratio < 0.5:
                    effectiveness = 0.6
                elif action == 'optimize' and state.engagement_rate_normalized > 0.6:
                    effectiveness = 0.7
                elif action == 'wait' and state.days_live_normalized < 0.3:
                    effectiveness = 0.5
                
                feedback = RLFeedbackSignal(
                    action_effectiveness=effectiveness,
                    cost_benefit_ratio=0.5,
                    urgency_score=max(0.0, 1.0 - state.baseline_gap_ratio),
                    intervention_type=action,
                    expected_improvement=effectiveness * 50000
                )
                
                feedback_signals.append(feedback)
                
            except Exception as e:
                print(f"[FactoryMetrics] Error computing feedback for {video_id}: {e}")
                continue
        
        return feedback_signals
    
    def get_latest_video_records(self) -> pd.DataFrame:
        """
        Get the latest record for each video while preserving full temporal data for growth analysis.
        
        Returns:
            pd.DataFrame: Latest records for each video, but temporal data remains available in self.video_stats
        """
        try:
            if self.video_stats.empty:
                return pd.DataFrame()
            
            # Get latest record for each video for current state analysis
            latest_records = self.video_stats.sort_values('timestamp').groupby('video_id').tail(1).reset_index(drop=True)
            
            return latest_records
        
        except Exception as e:
            print(f"[FactoryMetrics] Error getting latest video records: {e}")
            return pd.DataFrame()
    
    def compute_per_video_metrics(self, video_id: str) -> Optional[VideoKPIs]:
        """Compute detailed metrics for a single video using latest data."""
        try:
            if self.video_stats.empty or video_id not in self.video_stats['video_id'].values:
                print(f"[FactoryMetrics] Video {video_id} not found in stats")
                return None
            
            # Get the latest record for this video
            video_history = self.video_stats[self.video_stats['video_id'] == video_id].sort_values('timestamp')
            video = video_history.iloc[-1]  # Use latest record
            
            views = int(video['views'])
            likes = int(video['likes'])
            comments = int(video['comments'])
            shares = int(video['shares'])
            retention = float(video.get('retention_rate', 0.5))
            watch_time = float(video.get('watch_time_hours', views * 0.05))
            platform = str(video.get('platform', 'youtube'))
            publish_date = pd.to_datetime(video['publish_date'])
            timestamp = pd.to_datetime(video['timestamp'])
            
            days_live = max(1, (datetime.now() - publish_date).days)
            
            engagement_rate = self._calculate_engagement_rate(
                views, likes, comments, shares, platform
            )
            
            growth_velocity = self._calculate_growth_velocity(
                video_id, views, publish_date
            )
            
            virality_score = self._calculate_virality_score(
                video_id, views, engagement_rate, growth_velocity, 
                retention, days_live
            )
            
            if video_id not in self._ml_predictions_cache:
                self._load_ml_predictions([video_id])
            
            # Handle MLPrediction objects with confidence intervals
            prediction = self._ml_predictions_cache.get(video_id)
            if isinstance(prediction, MLPrediction):
                projected_final_views = prediction.predicted_views
            elif prediction:
                # Legacy compatibility for old cache format
                projected_final_views = prediction
            else:
                # Fallback to simple extrapolation
                projected_final_views = int(views + (growth_velocity * 30))
            
            meets_baseline = views >= self.baseline_views
            
            needs_intervention = (
                (days_live >= self.intervention_threshold_days and not meets_baseline) or
                (projected_final_views < self.baseline_views * 0.8) or
                (engagement_rate < self.target_engagement_rate * 0.5) or
                (retention < self.min_retention_rate * 0.7)
            )
            
            kpis = VideoKPIs(
                video_id=video_id,
                timestamp=timestamp,
                total_views=views,
                likes=likes,
                comments=comments,
                shares=shares,
                watch_time_hours=watch_time,
                avg_retention_rate=retention,
                engagement_rate=engagement_rate,
                growth_velocity=growth_velocity,
                virality_score=virality_score,
                days_since_publish=days_live,
                meets_baseline=meets_baseline,
                needs_intervention=needs_intervention,
                projected_final_views=projected_final_views
            )
            
            return kpis
            
        except Exception as e:
            print(f"[FactoryMetrics] Error computing metrics for {video_id}: {e}")
            return None
    
    def calculate_long_tail_distribution(self, video_kpis: List[VideoKPIs]) -> LongTailDistribution:
        """
        Calculate complete long-tail distribution analysis, not just a single scalar.
        
        Args:
            video_kpis: List of video KPIs to analyze
        
        Returns:
            LongTailDistribution: Complete distribution analysis with segments and percentiles
        """
        try:
            if not video_kpis:
                return LongTailDistribution(
                    total_videos=0,
                    distribution_percentiles={},
                    long_tail_segments={},
                    gini_coefficient=0.0,
                    power_law_alpha=0.0,
                    segment_counts={},
                    views_concentration={}
                )
            
            # Extract view counts for analysis
            view_counts = sorted([kpi.total_views for kpi in video_kpis])
            total_views = sum(view_counts)
            total_videos = len(video_kpis)
            
            # Calculate distribution percentiles
            percentiles = {}
            for p in [10, 25, 50, 75, 90, 95, 99]:
                percentile_value = np.percentile(view_counts, p)
                percentiles[f'p{p}'] = percentile_value
            
            # Calculate Gini coefficient (inequality measure)
            gini = self._calculate_gini_coefficient(view_counts)
            
            # Calculate power law exponent
            power_law_alpha = self._calculate_power_law_exponent(view_counts)
            
            # Define long-tail segments
            segments = {
                'micro': {'min_views': 0, 'max_views': 10000, 'description': 'Micro content (<10K views)'},
                'small': {'min_views': 10000, 'max_views': 100000, 'description': 'Small content (10K-100K views)'},
                'medium': {'min_views': 100000, 'max_views': 1000000, 'description': 'Medium content (100K-1M views)'},
                'large': {'min_views': 1000000, 'max_views': 10000000, 'description': 'Large content (1M-10M views)'},
                'mega': {'min_views': 10000000, 'max_views': float('inf'), 'description': 'Mega content (>10M views)'}
            }
            
            # Calculate segment statistics
            segment_counts = {}
            segment_views = {}
            segment_details = {}
            
            for segment_name, config in segments.items():
                min_views, max_views = config['min_views'], config['max_views']
                
                # Count videos in this segment
                count = len([v for v in view_counts if min_views <= v < max_views])
                segment_counts[segment_name] = count
                
                # Calculate total views in this segment
                segment_total = sum([v for v in view_counts if min_views <= v < max_views])
                segment_views[segment_name] = (segment_total / total_views) * 100 if total_views > 0 else 0
                
                segment_details[segment_name] = {
                    'count': count,
                    'total_views': segment_total,
                    'percentage': (count / total_videos) * 100 if total_videos > 0 else 0,
                    'views_percentage': segment_views[segment_name],
                    'description': config['description']
                }
            
            return LongTailDistribution(
                total_videos=total_videos,
                distribution_percentiles=percentiles,
                long_tail_segments=segment_details,
                gini_coefficient=gini,
                power_law_alpha=power_law_alpha,
                segment_counts=segment_counts,
                views_concentration=segment_views
            )
            
        except Exception as e:
            print(f"[FactoryMetrics] Error calculating long-tail distribution: {e}")
            return LongTailDistribution(
                total_videos=0,
                distribution_percentiles={},
                long_tail_segments={},
                gini_coefficient=0.0,
                power_law_alpha=0.0,
                segment_counts={},
                views_concentration={}
            )
    
    def _calculate_gini_coefficient(self, view_counts: List[int]) -> float:
        """Calculate Gini coefficient for inequality measurement."""
        try:
            if len(view_counts) < 2:
                return 0.0
            
            # Sort view counts
            sorted_views = sorted(view_counts)
            n = len(sorted_views)
            
            # Calculate Gini coefficient
            cumulative_sum = 0
            for i, views in enumerate(sorted_views):
                cumulative_sum += (i + 1) * views
            
            gini = (2 * cumulative_sum) / (n * sum(sorted_views)) - (n + 1) / n
            return max(0.0, min(1.0, gini))
            
        except Exception:
            return 0.0
    
    def _calculate_power_law_exponent(self, view_counts: List[int]) -> float:
        """Calculate power law exponent using maximum likelihood estimation."""
        try:
            if len(view_counts) < 10:
                return 0.0
            
            # Filter out zero views
            positive_views = [v for v in view_counts if v > 0]
            if len(positive_views) < 10:
                return 0.0
            
            # Use log-transformed data for power law fitting
            log_views = np.log(positive_views)
            log_ranks = np.log(range(1, len(positive_views) + 1))
            
            # Simple linear regression for power law exponent
            n = len(log_views)
            sum_x = sum(log_ranks)
            sum_y = sum(log_views)
            sum_xy = sum(x * y for x, y in zip(log_ranks, log_views))
            sum_x2 = sum(x * x for x in log_ranks)
            
            # Calculate slope (negative of power law exponent)
            alpha = -(n * sum_xy - sum_x * sum_y) / (n * sum_x2 - sum_x * sum_x)
            return max(0.0, alpha)
            
        except Exception:
            return 0.0
    
    def aggregate_time_bucket_metrics(
        self, 
        bucket_type: str, 
        start_date: datetime, 
        end_date: datetime,
        video_ids: Optional[List[str]] = None
    ) -> TimeBucketMetrics:
        """
        Aggregate metrics for a specific time bucket (daily/weekly/monthly).
        
        Args:
            bucket_type: 'daily', 'weekly', or 'monthly'
            start_date: Start date of the bucket
            end_date: End date of the bucket
            video_ids: Optional list of video IDs to include
        
        Returns:
            TimeBucketMetrics: Complete metrics for the time bucket
        """
        try:
            # Filter video stats for the time bucket
            bucket_stats = self.video_stats[
                (self.video_stats['timestamp'] >= start_date) & 
                (self.video_stats['timestamp'] <= end_date)
            ]
            
            if video_ids:
                bucket_stats = bucket_stats[bucket_stats['video_id'].isin(video_ids)]
            
            if bucket_stats.empty:
                return self._empty_time_bucket_metrics(bucket_type, start_date, end_date)
            
            # Get latest records for each video in the bucket
            latest_records = bucket_stats.sort_values('timestamp').groupby('video_id').tail(1)
            
            # Compute KPIs for videos in this bucket
            video_kpis = []
            for video_id in latest_records['video_id'].unique():
                kpi = self.compute_per_video_metrics(video_id)
                if kpi:
                    video_kpis.append(kpi)
            
            if not video_kpis:
                return self._empty_time_bucket_metrics(bucket_type, start_date, end_date)
            
            # Calculate aggregate metrics
            total_videos = len(video_kpis)
            total_views = sum(k.total_views for k in video_kpis)
            avg_views = total_views / total_videos
            avg_engagement = np.mean([k.engagement_rate for k in video_kpis])
            avg_velocity = np.mean([k.growth_velocity for k in video_kpis])
            avg_virality = np.mean([k.virality_score for k in video_kpis])
            
            videos_above_5m = sum(1 for k in video_kpis if k.total_views >= 5_000_000)
            videos_above_10m = sum(1 for k in video_kpis if k.total_views >= 10_000_000)
            videos_above_30m = sum(1 for k in video_kpis if k.total_views >= 30_000_000)
            
            baseline_compliance = (videos_above_5m / total_videos) * 100
            
            # Calculate long-tail distribution
            long_tail_dist = self.calculate_long_tail_distribution(video_kpis)
            
            # Get top and worst performers
            sorted_kpis = sorted(video_kpis, key=lambda k: k.total_views, reverse=True)
            top_performers = [
                {
                    'video_id': k.video_id,
                    'views': k.total_views,
                    'engagement_rate': k.engagement_rate,
                    'virality_score': k.virality_score,
                    'days_live': k.days_since_publish
                }
                for k in sorted_kpis[:10]
            ]
            
            worst_performers = [
                {
                    'video_id': k.video_id,
                    'views': k.total_views,
                    'engagement_rate': k.engagement_rate,
                    'virality_score': k.virality_score,
                    'days_live': k.days_since_publish
                }
                for k in sorted_kpis[-10:]
            ]
            
            return TimeBucketMetrics(
                bucket_type=bucket_type,
                bucket_start=start_date,
                bucket_end=end_date,
                total_videos=total_videos,
                total_views=total_views,
                avg_views_per_video=round(avg_views, 2),
                avg_engagement_rate=round(avg_engagement, 4),
                baseline_compliance_rate=round(baseline_compliance, 2),
                videos_above_5m=videos_above_5m,
                videos_above_10m=videos_above_10m,
                videos_above_30m=videos_above_30m,
                avg_growth_velocity=round(avg_velocity, 2),
                avg_virality_score=round(avg_virality, 2),
                long_tail_distribution=long_tail_dist,
                top_performers=top_performers,
                worst_performers=worst_performers
            )
            
        except Exception as e:
            print(f"[FactoryMetrics] Error aggregating {bucket_type} metrics: {e}")
            return self._empty_time_bucket_metrics(bucket_type, start_date, end_date)
    
    def _empty_time_bucket_metrics(self, bucket_type: str, start_date: datetime, end_date: datetime) -> TimeBucketMetrics:
        """Return empty time bucket metrics for edge cases."""
        empty_long_tail = LongTailDistribution(
            total_videos=0,
            distribution_percentiles={},
            long_tail_segments={},
            gini_coefficient=0.0,
            power_law_alpha=0.0,
            segment_counts={},
            views_concentration={}
        )
        
        return TimeBucketMetrics(
            bucket_type=bucket_type,
            bucket_start=start_date,
            bucket_end=end_date,
            total_videos=0,
            total_views=0,
            avg_views_per_video=0.0,
            avg_engagement_rate=0.0,
            baseline_compliance_rate=0.0,
            videos_above_5m=0,
            videos_above_10m=0,
            videos_above_30m=0,
            avg_growth_velocity=0.0,
            avg_virality_score=0.0,
            long_tail_distribution=empty_long_tail,
            top_performers=[],
            worst_performers=[]
        )
    
    def generate_time_series_aggregation(self, bucket_type: str = 'daily', num_buckets: int = 30, end_date: Optional[datetime] = None) -> List[TimeBucketMetrics]:
        """
        Generate time-series aggregation with proper handling of empty buckets.
        
        Args:
            bucket_type: Type of time bucket ('daily', 'weekly', 'monthly')
            num_buckets: Number of buckets to generate
            end_date: End date for the aggregation. If None, uses current date.
        
        Returns:
            List[TimeBucketMetrics]: List of time bucket metrics with continuity
        """
        try:
            if end_date is None:
                end_date = datetime.now()
            
            # Generate all time buckets first to ensure continuity
            time_buckets = []
            
            if bucket_type == 'daily':
                for i in range(num_buckets):
                    bucket_end = end_date - timedelta(days=i)
                    bucket_start = bucket_end - timedelta(days=1)
                    time_buckets.append((bucket_start, bucket_end))
            elif bucket_type == 'weekly':
                for i in range(num_buckets):
                    bucket_end = end_date - timedelta(weeks=i)
                    bucket_start = bucket_end - timedelta(weeks=1)
                    time_buckets.append((bucket_start, bucket_end))
            elif bucket_type == 'monthly':
                for i in range(num_buckets):
                    bucket_end = end_date - timedelta(days=30*i)
                    bucket_start = bucket_end - timedelta(days=30)
                    time_buckets.append((bucket_start, bucket_end))
            
            # Process each bucket
            bucket_metrics = []
            
            for bucket_start, bucket_end in time_buckets:
                # Get videos published in this bucket
                bucket_videos = []
                
                if not self.video_stats.empty:
                    # Filter videos by publish date within bucket range
                    bucket_mask = (
                        (pd.to_datetime(self.video_stats['publish_date']) >= bucket_start) &
                        (pd.to_datetime(self.video_stats['publish_date']) < bucket_end)
                    )
                    bucket_video_ids = self.video_stats[bucket_mask]['video_id'].unique()
                    
                    # Get KPIs for these videos
                    for video_id in bucket_video_ids:
                        kpi = self.compute_per_video_metrics(video_id)
                        if kpi:
                            bucket_videos.append(kpi)
                
                # Create bucket metrics (empty if no videos)
                if bucket_videos:
                    # Calculate metrics for this bucket
                    total_videos = len(bucket_videos)
                    total_views = sum(k.total_views for k in bucket_videos)
                    avg_views = total_views / total_videos
                    
                    avg_engagement = np.mean([k.engagement_rate for k in bucket_videos])
                    avg_velocity = np.mean([k.growth_velocity for k in bucket_videos])
                    avg_virality = np.mean([k.virality_score for k in bucket_videos])
                    
                    videos_above_5m = sum(1 for k in bucket_videos if k.total_views >= 5_000_000)
                    videos_above_10m = sum(1 for k in bucket_videos if k.total_views >= 10_000_000)
                    videos_above_30m = sum(1 for k in bucket_videos if k.total_views >= 30_000_000)
                    
                    baseline_compliance_rate = (videos_above_5m / total_videos) * 100
                    
                    # Calculate long-tail distribution
                    long_tail_dist = self.calculate_long_tail_distribution(bucket_videos)
                    
                    # Get top and worst performers
                    sorted_by_views = sorted(bucket_videos, key=lambda k: k.total_views, reverse=True)
                    top_performers = [
                        {
                            'video_id': k.video_id,
                            'views': k.total_views,
                            'engagement_rate': k.engagement_rate,
                            'virality_score': k.virality_score
                        }
                        for k in sorted_by_views[:10]
                    ]
                    
                    worst_performers = [
                        {
                            'video_id': k.video_id,
                            'views': k.total_views,
                            'engagement_rate': k.engagement_rate,
                            'virality_score': k.virality_score,
                            'gap_to_baseline': max(0, 5_000_000 - k.total_views)
                        }
                        for k in sorted_by_views[-10:]
                    ]
                    
                else:
                    # Empty bucket - create empty metrics
                    total_videos = 0
                    total_views = 0
                    avg_views = 0.0
                    avg_engagement = 0.0
                    avg_velocity = 0.0
                    avg_virality = 0.0
                    videos_above_5m = 0
                    videos_above_10m = 0
                    videos_above_30m = 0
                    baseline_compliance_rate = 0.0
                    
                    # Empty long-tail distribution
                    long_tail_dist = LongTailDistribution(
                        total_videos=0,
                        distribution_percentiles={},
                        long_tail_segments={},
                        gini_coefficient=0.0,
                        power_law_alpha=0.0,
                        segment_counts={},
                        views_concentration={}
                    )
                    
                    top_performers = []
                    worst_performers = []
                
                # Create TimeBucketMetrics object
                bucket_metric = TimeBucketMetrics(
                    bucket_type=bucket_type,
                    bucket_start=bucket_start,
                    bucket_end=bucket_end,
                    total_videos=total_videos,
                    total_views=total_views,
                    avg_views_per_video=avg_views,
                    avg_engagement_rate=avg_engagement,
                    baseline_compliance_rate=baseline_compliance_rate,
                    videos_above_5m=videos_above_5m,
                    videos_above_10m=videos_above_10m,
                    videos_above_30m=videos_above_30m,
                    avg_growth_velocity=avg_velocity,
                    avg_virality_score=avg_virality,
                    long_tail_distribution=long_tail_dist,
                    top_performers=top_performers,
                    worst_performers=worst_performers
                )
                
                bucket_metrics.append(bucket_metric)
            
            # Sort by date (newest first)
            bucket_metrics.sort(key=lambda x: x.bucket_start, reverse=True)
            
            return bucket_metrics
            
        except Exception as e:
            print(f"[FactoryMetrics] Error generating time series aggregation: {e}")
            return []
    
    def aggregate_factory_metrics(self) -> FactoryKPIs:
        """Aggregate KPIs across all videos in the niche using batch processing."""
        try:
            if self.video_stats.empty:
                print(f"[FactoryMetrics] No videos to aggregate for niche '{self.niche}'")
                return self._empty_factory_kpis()
            
            # Use batch processing for scalability
            batch_result = self.batch_compute_metrics()
            all_video_kpis = batch_result.video_kpis
            
            if not all_video_kpis:
                return self._empty_factory_kpis()
            
            total_videos = len(all_video_kpis)
            total_views = sum(k.total_views for k in all_video_kpis)
            avg_views = total_views / total_videos
            
            avg_engagement = np.mean([k.engagement_rate for k in all_video_kpis])
            avg_velocity = np.mean([k.growth_velocity for k in all_video_kpis])
            avg_virality = np.mean([k.virality_score for k in all_video_kpis])
            
            videos_above_5m = sum(1 for k in all_video_kpis if k.total_views >= 5_000_000)
            videos_above_10m = sum(1 for k in all_video_kpis if k.total_views >= 10_000_000)
            videos_above_30m = sum(1 for k in all_video_kpis if k.total_views >= 30_000_000)
            
            baseline_compliance_rate = (videos_above_5m / total_videos) * 100
            
            underperforming = sum(1 for k in all_video_kpis if k.needs_intervention)
            
            long_tail_potential = 0.0
            if self.long_tail_tracker:
                try:
                    # Calculate real long-tail potential using tracker
                    long_tail_scores = []
                    for kpi in all_video_kpis:
                        score = self.long_tail_tracker.compute_long_tail_score(kpi.video_id)
                        long_tail_scores.append(score)
                    
                    if long_tail_scores:
                        long_tail_potential = np.mean(long_tail_scores) * 100
                except Exception as e:
                    print(f"[FactoryMetrics] Error calculating long-tail potential: {e}")
                    # Fallback to heuristic
                    long_tail_videos = [
                        k for k in all_video_kpis 
                        if k.days_since_publish > 30 and k.growth_velocity > 1000
                    ]
                    long_tail_potential = (len(long_tail_videos) / total_videos) * 100
            else:
                # Fallback heuristic when long-tail tracker unavailable
                long_tail_videos = [
                    k for k in all_video_kpis 
                    if k.days_since_publish > 30 and k.growth_velocity > 1000
                ]
                long_tail_potential = (len(long_tail_videos) / total_videos) * 100
            
            recent_videos = [
                k for k in all_video_kpis 
                if k.days_since_publish <= 30
            ]
            daily_upload_rate = len(recent_videos) / 30
            
            factory_kpis = FactoryKPIs(
                niche=self.niche,
                timestamp=datetime.now(),
                total_videos=total_videos,
                total_views=total_views,
                avg_views_per_video=round(avg_views, 2),
                avg_engagement_rate=round(avg_engagement, 4),
                baseline_compliance_rate=round(baseline_compliance_rate, 2),
                videos_above_5m=videos_above_5m,
                videos_above_10m=videos_above_10m,
                videos_above_30m=videos_above_30m,
                avg_growth_velocity=round(avg_velocity, 2),
                avg_virality_score=round(avg_virality, 2),
                long_tail_potential=round(long_tail_potential, 2),
                underperforming_count=underperforming,
                daily_upload_rate=round(daily_upload_rate, 2)
            )
            
            self.kpi_summary = asdict(factory_kpis)
            
            print(f"[FactoryMetrics] Aggregated metrics for '{self.niche}' (batch processing):")
            print(f"  - Total Videos: {total_videos}")
            print(f"  - Total Views: {total_views:,}")
            print(f"  - Baseline Compliance: {baseline_compliance_rate:.1f}%")
            print(f"  - Underperforming: {underperforming}")
            print(f"  - Processing Time: {batch_result.processing_time_ms:.2f}ms")
            print(f"  - Throughput: {batch_result.throughput_per_second:.1f} videos/sec")
            
            return factory_kpis
            
        except Exception as e:
            print(f"[FactoryMetrics] Error aggregating factory metrics: {e}")
            return self._empty_factory_kpis()
    
    def _empty_factory_kpis(self) -> FactoryKPIs:
        """Return empty factory KPIs for edge cases."""
        return FactoryKPIs(
            niche=self.niche,
            timestamp=datetime.now(),
            total_videos=0,
            total_views=0,
            avg_views_per_video=0.0,
            avg_engagement_rate=0.0,
            baseline_compliance_rate=0.0,
            videos_above_5m=0,
            videos_above_10m=0,
            videos_above_30m=0,
            avg_growth_velocity=0.0,
            avg_virality_score=0.0,
            long_tail_potential=0.0,
            underperforming_count=0,
            daily_upload_rate=0.0
        )
    
    def get_sla_baseline_status(self, video_id: str) -> Dict:
        """
        Get SLA-based baseline status with time-to-baseline tracking.
        
        Args:
            video_id: Video ID to analyze
        
        Returns:
            Dict: SLA status with enforcement recommendations
        """
        try:
            if not self.sla_enforcement.get('enabled', False):
                return {'sla_enabled': False, 'message': 'SLA enforcement disabled'}
            
            kpi = self.compute_per_video_metrics(video_id)
            if not kpi:
                return {'error': 'Video not found', 'video_id': video_id}
            
            # Calculate time-to-baseline
            current_views = kpi.total_views
            days_live = kpi.days_since_publish
            time_to_baseline = self.sla_enforcement['time_to_baseline_days'] - days_live
            
            # Calculate required daily growth to meet baseline
            if time_to_baseline > 0:
                required_daily_growth = (self.baseline_views - current_views) / time_to_baseline
            else:
                required_daily_growth = 0
            
            # Calculate baseline probability
            baseline_probability = self._calculate_baseline_probability(kpi, self.baseline_views)
            
            # Determine enforcement level
            enforcement_level = 'low'
            for level, config in self.sla_enforcement['enforcement_levels'].items():
                if baseline_probability <= config['threshold']:
                    enforcement_level = level
                    break
            
            # Get SLA status
            sla_status = {
                'video_id': video_id,
                'sla_enabled': True,
                'baseline_views': self.baseline_views,
                'current_views': current_views,
                'days_live': days_live,
                'time_to_baseline_days': max(0, time_to_baseline),
                'required_daily_growth': required_daily_growth,
                'current_daily_growth': kpi.growth_velocity,
                'growth_gap': required_daily_growth - kpi.growth_velocity,
                'baseline_probability': baseline_probability,
                'enforcement_level': enforcement_level,
                'recommended_action': self.sla_enforcement['enforcement_levels'][enforcement_level]['action'],
                'meets_sla': baseline_probability >= self.sla_enforcement['baseline_probability_threshold'],
                'urgency_score': max(0.0, 1.0 - baseline_probability),
                'projected_final_views': kpi.projected_final_views,
                'confidence_interval': self._get_projection_confidence(video_id)
            }
            
            return sla_status
            
        except Exception as e:
            print(f"[FactoryMetrics] Error getting SLA baseline status for {video_id}: {e}")
            return {'error': str(e), 'video_id': video_id}
    
    def _get_projection_confidence(self, video_id: str) -> Tuple[float, float]:
        """Get confidence interval for projected final views."""
        try:
            prediction = self._ml_predictions_cache.get(video_id)
            if isinstance(prediction, MLPrediction):
                return (prediction.confidence_low, prediction.confidence_high)
            else:
                # Fallback confidence interval
                kpi = self.compute_per_video_metrics(video_id)
                if kpi:
                    projected = kpi.projected_final_views
                    confidence_range = int(projected * 0.2)
                    return max(0, projected - confidence_range), projected + confidence_range
            return (0, 0)
        except Exception:
            return (0, 0)
    
    def enforce_sla_compliance(self, video_ids: Optional[List[str]] = None) -> Dict:
        """
        Enforce SLA-based baseline compliance across videos.
        
        Args:
            video_ids: List of video IDs to check. If None, check all videos.
        
        Returns:
            Dict: SLA enforcement results and actions taken
        """
        try:
            if not self.sla_enforcement.get('enabled', False):
                return {'sla_enabled': False, 'message': 'SLA enforcement disabled'}
            
            if video_ids is None:
                video_ids = self.video_stats['video_id'].tolist()
            
            enforcement_results = {
                'total_videos_checked': len(video_ids),
                'sla_violations': 0,
                'critical_violations': 0,
                'high_violations': 0,
                'medium_violations': 0,
                'low_violations': 0,
                'actions_recommended': [],
                'videos_requiring_immediate_action': [],
                'sla_compliance_rate': 0.0
            }
            
            for video_id in video_ids:
                sla_status = self.get_sla_baseline_status(video_id)
                
                if 'error' in sla_status:
                    continue
                
                # Count violations by level
                if not sla_status['meets_sla']:
                    enforcement_results['sla_violations'] += 1
                    
                    if sla_status['enforcement_level'] == 'critical':
                        enforcement_results['critical_violations'] += 1
                        enforcement_results['videos_requiring_immediate_action'].append(video_id)
                    elif sla_status['enforcement_level'] == 'high':
                        enforcement_results['high_violations'] += 1
                    elif sla_status['enforcement_level'] == 'medium':
                        enforcement_results['medium_violations'] += 1
                    else:
                        enforcement_results['low_violations'] += 1
                    
                    enforcement_results['actions_recommended'].append({
                        'video_id': video_id,
                        'action': sla_status['recommended_action'],
                        'urgency': sla_status['urgency_score'],
                        'time_to_baseline': sla_status['time_to_baseline_days'],
                        'growth_gap': sla_status['growth_gap']
                    })
            
            # Calculate compliance rate
            if enforcement_results['total_videos_checked'] > 0:
                enforcement_results['sla_compliance_rate'] = (
                    (enforcement_results['total_videos_checked'] - enforcement_results['sla_violations']) / 
                    enforcement_results['total_videos_checked']
                ) * 100
            
            return enforcement_results
            
        except Exception as e:
            print(f"[FactoryMetrics] Error enforcing SLA compliance: {e}")
            return {'error': str(e)}
    
    def trigger_sla_interventions(self, video_ids: List[str]) -> Dict:
        """
        Trigger SLA-based interventions for videos not meeting baseline.
        
        Args:
            video_ids: List of video IDs requiring intervention
        
        Returns:
            Dict: Intervention results
        """
        try:
            intervention_results = {
                'videos_processed': 0,
                'interventions_triggered': 0,
                'boosters_applied': 0,
                'reposts_scheduled': 0,
                'optimizations_recommended': 0,
                'intervention_details': []
            }
            
            for video_id in video_ids:
                sla_status = self.get_sla_baseline_status(video_id)
                
                if 'error' in sla_status or sla_status.get('meets_sla', False):
                    continue
                
                intervention_results['videos_processed'] += 1
                action = sla_status['recommended_action']
                
                intervention_detail = {
                    'video_id': video_id,
                    'action': action,
                    'urgency': sla_status['urgency_score'],
                    'baseline_gap': self.baseline_views - sla_status['current_views'],
                    'time_to_baseline': sla_status['time_to_baseline_days'],
                    'growth_gap': sla_status['growth_gap']
                }
                
                if action == 'immediate_boost':
                    intervention_results['boosters_applied'] += 1
                    intervention_results['interventions_triggered'] += 1
                elif action == 'boost':
                    intervention_results['boosters_applied'] += 1
                    intervention_results['interventions_triggered'] += 1
                elif action == 'repost':
                    intervention_results['reposts_scheduled'] += 1
                    intervention_results['interventions_triggered'] += 1
                elif action == 'optimize':
                    intervention_results['optimizations_recommended'] += 1
                
                intervention_results['intervention_details'].append(intervention_detail)
            
            return intervention_results
            
        except Exception as e:
            print(f"[FactoryMetrics] Error triggering SLA interventions: {e}")
            return {'error': str(e)}
    
    def get_centralized_baseline_logic(self) -> Dict:
        """
        Get centralized baseline logic and configuration.
        
        Returns:
            Dict: Centralized baseline configuration
        """
        return {
            'baseline_views': self.baseline_views,
            'target_engagement_rate': self.target_engagement_rate,
            'min_retension_rate': self.min_retention_rate,
            'intervention_threshold_days': self.intervention_threshold_days,
            'sla_enforcement': self.sla_enforcement,
            'baseline_tiers': {
                '5M': self.baseline_views,
                '10M': self.baseline_views * 2,
                '30M': self.baseline_views * 6
            },
            'enforcement_levels': self.sla_enforcement['enforcement_levels'] if self.sla_enforcement.get('enabled') else {}
        }
    
    def _initialize_booster_queues(self) -> Dict[str, BoosterQueue]:
        """Initialize booster queues with configuration."""
        queues = {}
        
        for booster_type in ['immediate_boost', 'boost', 'repost', 'optimize']:
            queues[booster_type] = BoosterQueue(
                queue_name=f"{self.niche}_{booster_type}",
                requests=[],
                max_queue_size=self.booster_config['queue_capacity'][booster_type],
                processing_capacity=50,  # Base capacity, can be adjusted per type
                cooldown_rules=self.booster_config['cooldown_rules'],
                priority_weights=self.booster_config['priority_weights']
            )
        
        return queues
    
    def generate_booster_requests(self, video_ids: Optional[List[str]] = None) -> List[BoosterRequest]:
        """
        Generate booster requests for underperforming videos with proper SLA enforcement.
        
        Args:
            video_ids: List of video IDs to process. If None, process all videos.
        
        Returns:
            List[BoosterRequest]: Booster requests with priority and urgency
        """
        try:
            if not self.booster_config.get('enabled', False):
                return []
            
            # Get underperforming videos
            underperforming = self.identify_underperforming_videos(video_ids)
            
            if not underperforming:
                print(f"[FactoryMetrics] No underperforming videos found for booster requests")
                return []
            
            booster_requests = []
            current_time = datetime.now()
            
            for video in underperforming:
                video_id = video['video_id']
                
                # Get SLA status for this video
                video_kpis = self.compute_per_video_metrics(video_id)
                if not video_kpis:
                    continue
                
                sla_status = self.get_sla_baseline_status(video_id, video_kpis)
                
                # Map SLA enforcement level to booster type
                enforcement_action = sla_status['recommended_action']
                
                # Handle 'monitor' action - don't generate booster request
                if enforcement_action == 'monitor':
                    continue
                
                # Map action to booster type
                booster_type_mapping = {
                    'immediate_boost': 'immediate_boost',
                    'boost': 'boost',
                    'optimize': 'optimize',
                    'repost': 'repost'
                }
                
                booster_type = booster_type_mapping.get(enforcement_action, 'optimize')
                
                # Calculate priority based on SLA severity
                severity = sla_status['severity']
                if severity <= 0.3:
                    priority = 1  # Critical
                elif severity <= 0.6:
                    priority = 2  # High
                elif severity <= 0.8:
                    priority = 3  # Medium
                else:
                    priority = 4  # Low
                
                # Calculate urgency score
                urgency_score = 1.0 - severity  # Higher severity = higher urgency
                
                # Calculate expected impact
                gap_to_baseline = sla_status['gap_to_baseline']
                expected_impact = {
                    'views_increase': min(gap_to_baseline * 0.5, gap_to_baseline),
                    'engagement_improvement': 0.02,  # 2% improvement
                    'retention_improvement': 0.05   # 5% improvement
                }
                
                # Calculate cooldown period
                cooldown_rules = self.booster_config['cooldown_rules']
                cooldown_until = current_time + cooldown_rules.get(booster_type, timedelta(days=3))
                
                # Set retry count
                max_retries = self.booster_config['max_retries'].get(booster_type, 2)
                
                # Create booster request
                booster_request = BoosterRequest(
                    video_id=video_id,
                    booster_type=booster_type,
                    priority=priority,
                    urgency_score=urgency_score,
                    severity_score=severity * 100,  # Convert to 0-100 scale
                    expected_impact=expected_impact,
                    cooldown_until=cooldown_until,
                    retry_count=0,
                    max_retries=max_retries,
                    created_at=current_time,
                    expires_at=current_time + timedelta(hours=self.booster_config['expiration_hours']),
                    metadata={
                        'sla_status': sla_status,
                        'gap_to_baseline': gap_to_baseline,
                        'days_live': sla_status['days_live'],
                        'baseline_probability': sla_status['baseline_probability'],
                        'enforcement_level': sla_status['enforcement_level']
                    }
                )
                
                booster_requests.append(booster_request)
            
            # Sort by priority (lower number = higher priority)
            booster_requests.sort(key=lambda x: (x.priority, -x.urgency_score))
            
            print(f"[FactoryMetrics] Generated {len(booster_requests)} booster requests")
            return booster_requests
            
        except Exception as e:
            print(f"[FactoryMetrics] Error generating booster requests: {e}")
            return []
    
    def enqueue_booster_requests(self, requests: List[BoosterRequest]) -> Dict[str, int]:
        """
        Enqueue booster requests into priority queues with capacity limits.
        
        Args:
            requests: List of booster requests to enqueue
        
        Returns:
            Dict[str, int]: Enqueue results per queue type
        """
        try:
            enqueue_results = {
                'total_processed': 0,
                'enqueued': 0,
                'rejected_cooldown': 0,
                'rejected_capacity': 0,
                'rejected_expired': 0,
                'queue_breakdown': {}
            }
            
            now = datetime.now()
            
            for request in requests:
                # Check expiration
                if request.expires_at < now:
                    enqueue_results['rejected_expired'] += 1
                    continue
                
                # Check cooldown
                if request.cooldown_until > now:
                    enqueue_results['rejected_cooldown'] += 1
                    continue
                
                # Get appropriate queue
                queue = self.booster_queues.get(request.booster_type)
                if not queue:
                    continue
                
                # Check capacity
                if len(queue.requests) >= queue.max_queue_size:
                    enqueue_results['rejected_capacity'] += 1
                    continue
                
                # Enqueue request
                queue.requests.append(request)
                enqueue_results['enqueued'] += 1
                enqueue_results['total_processed'] += 1
                
                # Update queue breakdown
                if request.booster_type not in enqueue_results['queue_breakdown']:
                    enqueue_results['queue_breakdown'][request.booster_type] = 0
                enqueue_results['queue_breakdown'][request.booster_type] += 1
            
            # Sort each queue by priority
            for queue in self.booster_queues.values():
                queue.requests.sort(key=lambda x: (x.priority, -x.urgency_score, -x.severity_score))
            
            print(f"[FactoryMetrics] Enqueued {enqueue_results['enqueued']}/{enqueue_results['total_processed']} requests")
            return enqueue_results
            
        except Exception as e:
            print(f"[FactoryMetrics] Error enqueuing booster requests: {e}")
            return {'error': str(e)}
    
    def dequeue_booster_requests(self, queue_type: str, limit: int = 10) -> List[BoosterRequest]:
        """
        Dequeue highest priority booster requests for processing.
        
        Args:
            queue_type: Type of booster queue to process
            limit: Maximum number of requests to dequeue
        
        Returns:
            List[BoosterRequest]: Highest priority requests ready for processing
        """
        try:
            queue = self.booster_queues.get(queue_type)
            if not queue or not queue.requests:
                return []
            
            now = datetime.now()
            ready_requests = []
            
            # Filter out requests on cooldown or expired
            available_requests = [
                req for req in queue.requests 
                if req.cooldown_until <= now and req.expires_at > now
            ]
            
            # Take highest priority requests
            ready_requests = available_requests[:limit]
            
            # Remove from queue
            for request in ready_requests:
                if request in queue.requests:
                    queue.requests.remove(request)
            
            print(f"[FactoryMetrics] Dequeued {len(ready_requests)} requests from {queue_type} queue")
            return ready_requests
            
        except Exception as e:
            print(f"[FactoryMetrics] Error dequeuing from {queue_type} queue: {e}")
            return []
    
    def process_booster_response(self, response: BoosterResponse) -> bool:
        """
        Process booster execution response and handle retry logic.
        
        Args:
            response: Response from booster execution
        
        Returns:
            bool: True if response was processed successfully
        """
        try:
            if response.status == 'success':
                # Success - no retry needed
                print(f"[FactoryMetrics] Booster success for {response.video_id}: {response.booster_type}")
                return True
            
            elif response.status == 'cooldown':
                # Cooldown - reschedule for later
                if response.next_eligible_date:
                    # Could add to retry queue here
                    print(f"[FactoryMetrics] Booster cooldown for {response.video_id} until {response.next_eligible_date}")
                return True
            
            elif response.status == 'retry_later':
                # Retry logic
                # Find original request and increment retry count
                for queue in self.booster_queues.values():
                    for request in queue.requests:
                        if request.video_id == response.video_id and request.booster_type == response.booster_type:
                            if request.retry_count < request.max_retries:
                                request.retry_count += 1
                                # Increase priority for retry
                                request.priority = max(1, request.priority - 1)
                                print(f"[FactoryMetrics] Retrying booster for {response.video_id} (attempt {request.retry_count})")
                                return True
                            else:
                                # Max retries reached - remove from queue
                                queue.requests.remove(request)
                                print(f"[FactoryMetrics] Max retries reached for {response.video_id}")
                                return True
            
            elif response.status == 'failed':
                # Failed - log and remove
                print(f"[FactoryMetrics] Booster failed for {response.video_id}: {response.error_message}")
                # Remove from queue
                for queue in self.booster_queues.values():
                    queue.requests = [req for req in queue.requests 
                                   if not (req.video_id == response.video_id and req.booster_type == response.booster_type)]
                return True
            
            return False
            
        except Exception as e:
            print(f"[FactoryMetrics] Error processing booster response: {e}")
            return False
    
    def get_booster_queue_status(self) -> Dict[str, Dict]:
        """
        Get current status of all booster queues.
        
        Returns:
            Dict[str, Dict]: Status of each booster queue
        """
        try:
            queue_status = {}
            now = datetime.now()
            
            for queue_type, queue in self.booster_queues.items():
                total_requests = len(queue.requests)
                ready_requests = len([req for req in queue.requests if req.cooldown_until <= now and req.expires_at > now])
                cooldown_requests = len([req for req in queue.requests if req.cooldown_until > now])
                expired_requests = len([req for req in queue.requests if req.expires_at <= now])
                
                # Calculate priority distribution
                priority_dist = {}
                for request in queue.requests:
                    priority = request.priority
                    if priority not in priority_dist:
                        priority_dist[priority] = 0
                    priority_dist[priority] += 1
                
                queue_status[queue_type] = {
                    'queue_size': total_requests,
                    'ready_for_processing': ready_requests,
                    'on_cooldown': cooldown_requests,
                    'expired': expired_requests,
                    'capacity_utilization': (total_requests / queue.max_queue_size) * 100,
                    'priority_distribution': priority_dist,
                    'oldest_request_age': (now - min(req.created_at for req in queue.requests)).total_seconds() / 3600 if queue.requests else 0,
                    'newest_request_age': (now - max(req.created_at for req in queue.requests)).total_seconds() / 3600 if queue.requests else 0
                }
            
            return queue_status
            
        except Exception as e:
            print(f"[FactoryMetrics] Error getting booster queue status: {e}")
            return {}
    
    def get_booster_lifecycle_signals(self) -> Dict[str, List[Dict]]:
        """
        Generate lifecycle signals for posting strategy optimization.
        
        Returns:
            Dict[str, List[Dict]]: Lifecycle signals by video category
        """
        try:
            lifecycle_signals = {
                'critical_intervention': [],
                'growth_opportunity': [],
                'cooldown_period': [],
                'retry_candidates': [],
                'optimization_needed': []
            }
            
            now = datetime.now()
            
            # Analyze all videos in queues
            for queue_type, queue in self.booster_queues.items():
                for request in queue.requests:
                    signal = {
                        'video_id': request.video_id,
                        'booster_type': request.booster_type,
                        'priority': request.priority,
                        'urgency': request.urgency_score,
                        'severity': request.severity_score,
                        'retry_count': request.retry_count,
                        'max_retries': request.max_retries,
                        'created_at': request.created_at.isoformat(),
                        'expires_at': request.expires_at.isoformat(),
                        'cooldown_until': request.cooldown_until.isoformat(),
                        'expected_impact': request.expected_impact,
                        'metadata': request.metadata
                    }
                    
                    # Categorize signals
                    if request.priority <= 2 and request.urgency_score >= 0.7:
                        lifecycle_signals['critical_intervention'].append(signal)
                    elif request.urgency_score >= 0.5:
                        lifecycle_signals['growth_opportunity'].append(signal)
                    elif request.cooldown_until > now:
                        lifecycle_signals['cooldown_period'].append(signal)
                    elif request.retry_count > 0:
                        lifecycle_signals['retry_candidates'].append(signal)
                    else:
                        lifecycle_signals['optimization_needed'].append(signal)
            
            # Sort each category by priority
            for category in lifecycle_signals:
                lifecycle_signals[category].sort(key=lambda x: (x['priority'], -x['urgency']))
            
            return lifecycle_signals
            
        except Exception as e:
            print(f"[FactoryMetrics] Error generating lifecycle signals: {e}")
            return {}
    
    def identify_underperforming_videos(
        self,
        min_days_live: int = 7,
        max_results: int = 100
    ) -> List[Dict]:
        """Identify videos not meeting baseline KPIs (5 million views)."""
        try:
            if self.video_stats.empty:
                return []
            
            underperforming = []
            
            for video_id in self.video_stats['video_id']:
                kpi = self.compute_per_video_metrics(video_id)
                
                if not kpi:
                    continue
                
                if kpi.days_since_publish < min_days_live:
                    continue
                
                if kpi.needs_intervention:
                    underperforming.append({
                        'video_id': video_id,
                        'current_views': kpi.total_views,
                        'projected_views': kpi.projected_final_views,
                        'days_live': kpi.days_since_publish,
                        'engagement_rate': kpi.engagement_rate,
                        'retention_rate': kpi.avg_retention_rate,
                        'growth_velocity': kpi.growth_velocity,
                        'virality_score': kpi.virality_score,
                        'gap_to_baseline': self.baseline_views - kpi.total_views,
                        'severity': self._calculate_intervention_severity(kpi),
                        'recommended_actions': self._recommend_interventions(kpi)
                    })
            
            underperforming.sort(key=lambda x: x['severity'], reverse=True)
            underperforming = underperforming[:max_results]
            
            print(f"[FactoryMetrics] Identified {len(underperforming)} underperforming videos")
            
            return underperforming
            
        except Exception as e:
            print(f"[FactoryMetrics] Error identifying underperforming videos: {e}")
            return []
    
    def _calculate_intervention_severity(self, kpi: VideoKPIs) -> float:
        """Calculate how urgently a video needs intervention (0-100)."""
        severity = 0.0
        
        if kpi.total_views < self.baseline_views:
            gap_ratio = 1 - (kpi.total_views / self.baseline_views)
            severity += gap_ratio * 40
        
        if kpi.days_since_publish > 30:
            time_score = min(30, (kpi.days_since_publish / 60) * 30)
            severity += time_score
        
        if kpi.engagement_rate < self.target_engagement_rate * 0.5:
            severity += 10
        if kpi.avg_retention_rate < self.min_retention_rate * 0.7:
            severity += 10
        
        expected_daily = self.baseline_views / 30
        if kpi.growth_velocity < expected_daily * 0.3:
            severity += 10
        
        return round(severity, 2)
    
    def _recommend_interventions(self, kpi: VideoKPIs) -> List[str]:
        """Recommend specific interventions based on video KPIs."""
        actions = []
        
        if kpi.total_views < self.baseline_views * 0.5:
            actions.append("CRITICAL: Apply paid boosters immediately")
            actions.append("Consider reposting with optimized title/thumbnail")
        elif kpi.total_views < self.baseline_views:
            actions.append("Apply moderate boosters")
        
        if kpi.engagement_rate < self.target_engagement_rate * 0.7:
            actions.append("Test new thumbnails to improve CTR")
            actions.append("Add engagement hooks in description")
        
        if kpi.avg_retention_rate < self.min_retention_rate:
            actions.append("Analyze retention curve for drop-off points")
            actions.append("Consider re-editing with faster pacing")
        
        expected_daily = self.baseline_views / 30
        if kpi.growth_velocity < expected_daily * 0.5:
            actions.append("Increase posting frequency around this topic")
            actions.append("Cross-promote with better-performing videos")
        
        if kpi.days_since_publish > 30 and not kpi.meets_baseline:
            actions.append("Consider archiving or repurposing content")
        
        return actions
    
    def push_metrics_to_dashboard_api(self, factory_kpis: FactoryKPIs, underperforming: List[Dict]) -> DashboardAPIResponse:
        """
        Push metrics to the actual dashboard API with proper schema validation and error handling.
        
        Args:
            factory_kpis: Aggregated factory KPIs
            underperforming: List of underperforming video data
        
        Returns:
            DashboardAPIResponse: API response with status and metadata
        """
        try:
            if not self.dashboard_config.get('enabled', False):
                return DashboardAPIResponse(
                    success=False,
                    status_code=0,
                    response_time_ms=0.0,
                    message="Dashboard API integration disabled",
                    data_sent={},
                    api_version=self.dashboard_schema.schema_version,
                    timestamp=datetime.now()
                )
            
            start_time = datetime.now()
            
            # Prepare dashboard payload according to schema
            dashboard_payload = self._prepare_dashboard_payload(factory_kpis, underperforming)
            
            # Validate payload against schema
            validation_result = self._validate_dashboard_payload(dashboard_payload)
            if not validation_result['valid']:
                return DashboardAPIResponse(
                    success=False,
                    status_code=400,
                    response_time_ms=(datetime.now() - start_time).total_seconds() * 1000,
                    message=f"Schema validation failed: {validation_result['errors']}",
                    data_sent=dashboard_payload,
                    api_version=self.dashboard_schema.schema_version,
                    timestamp=datetime.now(),
                    error_details=str(validation_result['errors'])
                )
            
            # Make API call
            api_response = self._make_dashboard_api_call(dashboard_payload)
            
            response_time = (datetime.now() - start_time).total_seconds() * 1000
            
            # Create response object
            dashboard_response = DashboardAPIResponse(
                success=api_response.get('success', False),
                status_code=api_response.get('status_code', 500),
                response_time_ms=response_time,
                message=api_response.get('message', 'API call completed'),
                data_sent=dashboard_payload,
                api_version=self.dashboard_schema.schema_version,
                timestamp=datetime.now(),
                error_details=api_response.get('error')
            )
            
            # Log the API call result
            self._log_dashboard_api_call(dashboard_response)
            
            # If API call failed and fallback is enabled, save to file
            if not dashboard_response.success and self.dashboard_config.get('fallback_to_file', True):
                self._fallback_to_file(dashboard_payload, underperforming)
            
            return dashboard_response
            
        except Exception as e:
            print(f"[FactoryMetrics] Error pushing metrics to dashboard API: {e}")
            return DashboardAPIResponse(
                success=False,
                status_code=500,
                response_time_ms=0.0,
                message=f"Internal error: {str(e)}",
                data_sent={},
                api_version=self.dashboard_schema.schema_version,
                timestamp=datetime.now(),
                error_details=str(e)
            )
    
    def _prepare_dashboard_payload(self, factory_kpis: FactoryKPIs, underperforming: List[Dict]) -> Dict[str, Any]:
        """Prepare dashboard payload according to schema field mappings."""
        try:
            # Map factory KPIs to dashboard schema
            payload = {}
            
            # Map required fields
            for schema_field, factory_field in self.dashboard_schema.field_mappings.items():
                if hasattr(factory_kpis, factory_field):
                    payload[schema_field] = getattr(factory_kpis, factory_field)
            
            # Add timestamp in ISO format
            payload['timestamp'] = factory_kpis.timestamp.isoformat()
            
            # Add underperforming videos
            payload['underperforming_videos'] = underperforming
            
            # Add metadata
            payload['metadata'] = {
                'schema_version': self.dashboard_schema.schema_version,
                'source': 'factory_metrics',
                'niche': self.niche,
                'generated_at': datetime.now().isoformat(),
                'data_quality': {
                    'total_videos': factory_kpis.total_videos,
                    'data_freshness_hours': (datetime.now() - factory_kpis.timestamp).total_seconds() / 3600
                }
            }
            
            # Add engagement drop alerts
            engagement_alerts = self._detect_engagement_drops(factory_kpis, underperforming)
            if engagement_alerts:
                payload['alerts'] = engagement_alerts
            
            return payload
            
        except Exception as e:
            print(f"[FactoryMetrics] Error preparing dashboard payload: {e}")
            return {}
    
    def _validate_dashboard_payload(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Validate payload against dashboard schema requirements."""
        try:
            validation_result = {
                'valid': True,
                'errors': [],
                'warnings': []
            }
            
            # Check required fields
            for required_field in self.dashboard_schema.required_fields:
                if required_field not in payload:
                    validation_result['valid'] = False
                    validation_result['errors'].append(f"Missing required field: {required_field}")
            
            # Check for deprecated fields
            for deprecated_field in self.dashboard_schema.deprecated_fields:
                if deprecated_field in payload:
                    validation_result['warnings'].append(f"Deprecated field used: {deprecated_field}")
            
            # Validate data types
            if 'total_videos' in payload and not isinstance(payload['total_videos'], int):
                validation_result['valid'] = False
                validation_result['errors'].append("total_videos must be an integer")
            
            if 'total_views' in payload and not isinstance(payload['total_views'], int):
                validation_result['valid'] = False
                validation_result['errors'].append("total_views must be an integer")
            
            if 'avg_engagement_rate' in payload:
                try:
                    float(payload['avg_engagement_rate'])
                except (ValueError, TypeError):
                    validation_result['valid'] = False
                    validation_result['errors'].append("avg_engagement_rate must be a number")
            
            return validation_result
            
        except Exception as e:
            return {
                'valid': False,
                'errors': [f"Validation error: {str(e)}"],
                'warnings': []
            }
    
    def _make_dashboard_api_call(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Make actual API call to dashboard with retry logic."""
        try:
            import requests
            import os
            
            # Prepare headers
            headers = {
                'Content-Type': 'application/json',
                'User-Agent': f'FactoryMetrics/{self.dashboard_schema.schema_version}'
            }
            
            # Add authentication if required
            if self.dashboard_schema.authentication_required:
                api_key = self.dashboard_config.get('api_key') or os.getenv('DASHBOARD_API_KEY')
                if api_key:
                    headers['Authorization'] = f'Bearer {api_key}'
                else:
                    return {
                        'success': False,
                        'status_code': 401,
                        'message': 'Authentication required but no API key provided',
                        'error': 'Missing API key'
                    }
            
            # Make API call with retry logic
            max_retries = self.dashboard_config.get('retry_attempts', 3)
            timeout = self.dashboard_config.get('timeout_seconds', 30)
            
            for attempt in range(max_retries):
                try:
                    response = requests.post(
                        self.dashboard_schema.api_endpoint,
                        json=payload,
                        headers=headers,
                        timeout=timeout
                    )
                    
                    if response.status_code == 200:
                        return {
                            'success': True,
                            'status_code': response.status_code,
                            'message': 'Metrics successfully pushed to dashboard',
                            'response': response.json() if response.content else {}
                        }
                    else:
                        # Handle different error codes
                        if response.status_code == 401:
                            return {
                                'success': False,
                                'status_code': response.status_code,
                                'message': 'Authentication failed',
                                'error': 'Invalid API key'
                            }
                        elif response.status_code == 400:
                            return {
                                'success': False,
                                'status_code': response.status_code,
                                'message': 'Bad request - invalid payload',
                                'error': response.text
                            }
                        elif response.status_code == 429:
                            # Rate limited - wait and retry
                            if attempt < max_retries - 1:
                                wait_time = 2 ** attempt  # Exponential backoff
                                print(f"[FactoryMetrics] Rate limited, waiting {wait_time}s before retry {attempt + 1}")
                                import time
                                time.sleep(wait_time)
                                continue
                            else:
                                return {
                                    'success': False,
                                    'status_code': response.status_code,
                                    'message': 'Rate limit exceeded after retries',
                                    'error': 'Too many requests'
                                }
                        else:
                            # Other server errors - retry
                            if attempt < max_retries - 1 and response.status_code >= 500:
                                wait_time = 2 ** attempt
                                print(f"[FactoryMetrics] Server error {response.status_code}, retrying in {wait_time}s")
                                import time
                                time.sleep(wait_time)
                                continue
                            else:
                                return {
                                    'success': False,
                                    'status_code': response.status_code,
                                    'message': f'API call failed: {response.status_code}',
                                    'error': response.text
                                }
                
                except requests.exceptions.Timeout:
                    if attempt < max_retries - 1:
                        wait_time = 2 ** attempt
                        print(f"[FactoryMetrics] Timeout, retrying in {wait_time}s")
                        import time
                        time.sleep(wait_time)
                        continue
                    else:
                        return {
                            'success': False,
                            'status_code': 408,
                            'message': 'Request timeout after retries',
                            'error': 'Timeout'
                        }
                
                except requests.exceptions.ConnectionError:
                    if attempt < max_retries - 1:
                        wait_time = 2 ** attempt
                        print(f"[FactoryMetrics] Connection error, retrying in {wait_time}s")
                        import time
                        time.sleep(wait_time)
                        continue
                    else:
                        return {
                            'success': False,
                            'status_code': 503,
                            'message': 'Connection failed after retries',
                            'error': 'Connection error'
                        }
            
            # Should not reach here
            return {
                'success': False,
                'status_code': 500,
                'message': 'Unknown error during API call',
                'error': 'Unexpected error'
            }
            
        except ImportError:
            # requests not available - fallback to file
            return {
                'success': False,
                'status_code': 0,
                'message': 'requests library not available',
                'error': 'Missing dependency'
            }
        except Exception as e:
            return {
                'success': False,
                'status_code': 500,
                'message': f'API call error: {str(e)}',
                'error': str(e)
            }
    
    def _detect_engagement_drops(self, factory_kpis: FactoryKPIs, underperforming: List[Dict]) -> List[Dict]:
        """Detect engagement drops and create alerts."""
        try:
            alerts = []
            
            # Check overall engagement rate drop
            if factory_kpis.avg_engagement_rate < self.target_engagement_rate * 0.7:
                alerts.append({
                    'type': 'engagement_drop',
                    'severity': 'HIGH',
                    'message': f"Overall engagement rate at {factory_kpis.avg_engagement_rate:.3f} (target: {self.target_engagement_rate:.3f})",
                    'metric': 'avg_engagement_rate',
                    'current_value': factory_kpis.avg_engagement_rate,
                    'target_value': self.target_engagement_rate,
                    'drop_percentage': ((self.target_engagement_rate - factory_kpis.avg_engagement_rate) / self.target_engagement_rate) * 100,
                    'recommended_action': 'Review content strategy and engagement hooks',
                    'affected_videos': factory_kpis.total_videos
                })
            
            # Check baseline compliance rate drop
            if factory_kpis.baseline_compliance_rate < 70:
                alerts.append({
                    'type': 'baseline_compliance_drop',
                    'severity': 'CRITICAL',
                    'message': f"Baseline compliance at {factory_kpis.baseline_compliance_rate:.1f}% (target: 80%+)",
                    'metric': 'baseline_compliance_rate',
                    'current_value': factory_kpis.baseline_compliance_rate,
                    'target_value': 80.0,
                    'drop_percentage': 80.0 - factory_kpis.baseline_compliance_rate,
                    'recommended_action': 'Scale up booster budget or adjust content formula',
                    'affected_videos': factory_kpis.total_videos
                })
            
            # Check underperforming video count spike
            if factory_kpis.underperforming_count > factory_kpis.total_videos * 0.3:
                alerts.append({
                    'type': 'underperforming_spike',
                    'severity': 'MEDIUM',
                    'message': f"{factory_kpis.underperforming_count} videos need intervention ({(factory_kpis.underperforming_count / factory_kpis.total_videos) * 100:.1f}%)",
                    'metric': 'underperforming_count',
                    'current_value': factory_kpis.underperforming_count,
                    'target_value': factory_kpis.total_videos * 0.3,
                    'drop_percentage': ((factory_kpis.underperforming_count - factory_kpis.total_videos * 0.3) / (factory_kpis.total_videos * 0.3)) * 100,
                    'recommended_action': 'Review content quality and posting schedule',
                    'affected_videos': factory_kpis.underperforming_count
                })
            
            # Check individual video engagement drops
            critical_engagement_videos = [
                video for video in underperforming 
                if video.get('engagement_rate', 0) < self.target_engagement_rate * 0.5
            ]
            
            if len(critical_engagement_videos) > 5:
                alerts.append({
                    'type': 'individual_engagement_drops',
                    'severity': 'HIGH',
                    'message': f"{len(critical_engagement_videos)} videos with critically low engagement (<50% of target)",
                    'metric': 'individual_engagement_rate',
                    'current_value': len(critical_engagement_videos),
                    'target_value': 5,
                    'drop_percentage': ((len(critical_engagement_videos) - 5) / 5) * 100 if len(critical_engagement_videos) > 5 else 0,
                    'recommended_action': 'Test new thumbnails and content hooks for affected videos',
                    'affected_videos': len(critical_engagement_videos),
                    'video_ids': [video['video_id'] for video in critical_engagement_videos[:10]]  # Top 10
                })
            
            return alerts
            
        except Exception as e:
            print(f"[FactoryMetrics] Error detecting engagement drops: {e}")
            return []
    
    def _log_dashboard_api_call(self, response: DashboardAPIResponse):
        """Log dashboard API call results for monitoring."""
        try:
            log_entry = {
                'timestamp': response.timestamp.isoformat(),
                'api_version': response.api_version,
                'success': response.success,
                'status_code': response.status_code,
                'response_time_ms': response.response_time_ms,
                'message': response.message,
                'data_size': len(str(response.data_sent)),
                'niche': self.niche
            }
            
            if response.error_details:
                log_entry['error_details'] = response.error_details
            
            # Try to use structured logger
            try:
                from infra.logger import log_event
                log_event(f"[{self.niche} Dashboard API] {json.dumps(log_entry)}")
            except ImportError:
                # Fallback to file logging
                log_dir = self.data_dir / self.niche / "logs"
                log_dir.mkdir(parents=True, exist_ok=True)
                
                log_file = log_dir / "dashboard_api.log"
                with open(log_file, 'a') as f:
                    f.write(json.dumps(log_entry) + '\n')
            
            print(f"[FactoryMetrics] Dashboard API call: {response.success} ({response.status_code}) - {response.response_time_ms:.0f}ms")
            
        except Exception as e:
            print(f"[FactoryMetrics] Error logging dashboard API call: {e}")
    
    def _fallback_to_file(self, payload: Dict[str, Any], underperforming: List[Dict]):
        """Fallback to file-based storage when API is unavailable."""
        try:
            output_dir = self.data_dir / self.niche / "dashboard"
            output_dir.mkdir(parents=True, exist_ok=True)
            
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            output_file = output_dir / f"metrics_fallback_{timestamp}.json"
            
            fallback_data = {
                'payload': payload,
                'underperforming': underperforming,
                'fallback_reason': 'API unavailable',
                'fallback_timestamp': datetime.now().isoformat(),
                'schema_version': self.dashboard_schema.schema_version
            }
            
            with open(output_file, 'w') as f:
                json.dump(fallback_data, f, indent=2, default=str)
            
            print(f"[FactoryMetrics] Fallback: Saved metrics to {output_file}")
            
        except Exception as e:
            print(f"[FactoryMetrics] Error in fallback to file: {e}")
    
    def push_metrics_to_dashboard(self, dashboard_api_url: Optional[str] = None):
        """
        Send aggregated and per-video KPIs to the dashboard API with proper integration.
        
        This method now uses the actual dashboard API instead of just file dumps.
        """
        try:
            factory_kpis = self.aggregate_factory_metrics()
            underperforming = self.identify_underperforming_videos()
            
            # Use the new API integration
            api_response = self.push_metrics_to_dashboard_api(factory_kpis, underperforming)
            
            # Create alerts for critical conditions
            self._create_alerts(factory_kpis, underperforming)
            
            # Log the result
            if api_response.success:
                print(f"[FactoryMetrics] Successfully pushed metrics to dashboard API")
                print(f"  - Status: {api_response.status_code}")
                print(f"  - Response time: {api_response.response_time_ms:.0f}ms")
                print(f"  - API version: {api_response.api_version}")
            else:
                print(f"[FactoryMetrics] Failed to push metrics to dashboard API")
                print(f"  - Status: {api_response.status_code}")
                print(f"  - Error: {api_response.error_details or api_response.message}")
                if self.dashboard_config.get('fallback_to_file', True):
                    print(f"  - Fallback: Saved to file system")
            
            return api_response
            
        except Exception as e:
            print(f"[FactoryMetrics] Error pushing metrics to dashboard: {e}")
            # Return error response
            return DashboardAPIResponse(
                success=False,
                status_code=500,
                response_time_ms=0.0,
                message=f"Internal error: {str(e)}",
                data_sent={},
                api_version=self.dashboard_schema.schema_version,
                timestamp=datetime.now(),
                error_details=str(e)
            )
    
    def _create_alerts(self, factory_kpis: FactoryKPIs, underperforming: List[Dict]):
        """Create alerts for critical conditions."""
        alerts = []
        
        if factory_kpis.baseline_compliance_rate < 70:
            alerts.append({
                'severity': 'HIGH',
                'type': 'baseline_compliance',
                'message': f"Baseline compliance at {factory_kpis.baseline_compliance_rate:.1f}% (target: 80%+)",
                'action': 'Review content strategy and booster allocation'
            })
        
        if factory_kpis.underperforming_count > factory_kpis.total_videos * 0.3:
            alerts.append({
                'severity': 'MEDIUM',
                'type': 'underperforming_videos',
                'message': f"{factory_kpis.underperforming_count} videos need intervention",
                'action': 'Scale up booster budget or adjust content formula'
            })
        
        if factory_kpis.avg_engagement_rate < self.target_engagement_rate * 0.7:
            alerts.append({
                'severity': 'MEDIUM',
                'type': 'engagement',
                'message': f"Avg engagement at {factory_kpis.avg_engagement_rate:.3f} (target: {self.target_engagement_rate:.3f})",
                'action': 'Test new hooks and thumbnail strategies'
            })
        
        if alerts:
            alerts_dir = self.data_dir / self.niche / "alerts"
            alerts_dir.mkdir(parents=True, exist_ok=True)
            
            alert_file = alerts_dir / f"alerts_{datetime.now().strftime('%Y%m%d')}.json"
            
            with open(alert_file, 'w') as f:
                json.dump(alerts, f, indent=2)
            
            print(f"[FactoryMetrics] Created {len(alerts)} alerts")
    
    def log_metrics(self):
        """Log all key metrics centrally for auditing and scaling analysis."""
        try:
            if not self.kpi_summary:
                self.aggregate_factory_metrics()
            
            try:
                from infra.logger import log_event
                log_event(
                    f"[{self.niche} Metrics] Aggregated KPIs: {json.dumps(self.kpi_summary, indent=2)}"
                )
            except ImportError:
                pass
            
            log_dir = self.data_dir / self.niche / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            
            log_file = log_dir / f"metrics_{datetime.now().strftime('%Y%m%d')}.jsonl"
            
            with open(log_file, 'a') as f:
                log_entry = {
                    'timestamp': datetime.now().isoformat(),
                    'niche': self.niche,
                    'kpis': self.kpi_summary
                }
                f.write(json.dumps(log_entry) + '\n')
            
            print(f"[FactoryMetrics] Logged metrics to {log_file}")
            
        except Exception as e:
            print(f"[FactoryMetrics] Error logging metrics: {e}")
    
    def get_temporal_aggregates(self, period: str = 'daily', days_back: int = 30) -> Dict:
        """
        Get temporal aggregates with daily/weekly/monthly bins and rolling windows.
        
        Args:
            period: 'daily', 'weekly', or 'monthly'
            days_back: Number of days to look back
        
        Returns:
            Dict: Temporal aggregates with rolling windows
        """
        try:
            if self.video_stats.empty:
                return {}
            
            # Convert timestamps and filter by date range
            df = self.video_stats.copy()
            df['timestamp'] = pd.to_datetime(df['timestamp'])
            cutoff_date = datetime.now() - pd.Timedelta(days=days_back)
            df = df[df['timestamp'] >= cutoff_date]
            
            if df.empty:
                return {}
            
            # Group by period
            if period == 'daily':
                df['period'] = df['timestamp'].dt.date
                group_col = 'period'
            elif period == 'weekly':
                df['period'] = df['timestamp'].dt.to_period('W').dt.start_time
                group_col = 'period'
            elif period == 'monthly':
                df['period'] = df['timestamp'].dt.to_period('M').dt.start_time
                group_col = 'period'
            else:
                raise ValueError(f"Invalid period: {period}")
            
            # Calculate aggregates per period
            aggregates = {}
            for period_name, group in df.groupby(group_col):
                period_kpis = []
                
                for _, video in group.iterrows():
                    kpi = self.compute_per_video_metrics(video['video_id'])
                    if kpi:
                        period_kpis.append(kpi)
                
                if period_kpis:
                    aggregates[str(period_name)] = {
                        'total_videos': len(period_kpis),
                        'total_views': sum(k.total_views for k in period_kpis),
                        'avg_engagement': np.mean([k.engagement_rate for k in period_kpis]),
                        'baseline_compliance': sum(1 for k in period_kpis if k.meets_baseline) / len(period_kpis) * 100,
                        'underperforming_count': sum(1 for k in period_kpis if k.needs_intervention),
                        'avg_virality': np.mean([k.virality_score for k in period_kpis])
                    }
            
            # Calculate rolling averages
            if len(aggregates) > 1:
                periods = sorted(aggregates.keys())
                rolling_window = min(7, len(periods))  # 7-period rolling window
                
                for i, period in enumerate(periods):
                    start_idx = max(0, i - rolling_window + 1)
                    window_periods = periods[start_idx:i+1]
                    
                    if len(window_periods) > 1:
                        window_data = [aggregates[p] for p in window_periods]
                        aggregates[period]['rolling_avg_views'] = np.mean([d['total_views'] for d in window_data])
                        aggregates[period]['rolling_avg_engagement'] = np.mean([d['avg_engagement'] for d in window_data])
                        aggregates[period]['rolling_baseline_compliance'] = np.mean([d['baseline_compliance'] for d in window_data])
            
            return aggregates
            
        except Exception as e:
            print(f"[FactoryMetrics] Error getting temporal aggregates: {e}")
            return {}
    
    def get_historical_snapshots(self, days_back: int = 90) -> List[Dict]:
        """
        Get historical KPI snapshots per factory for trend analysis.
        
        Args:
            days_back: Number of days to look back
        
        Returns:
            List[Dict]: Historical snapshots
        """
        try:
            snapshots = []
            current_date = datetime.now()
            
            for days_ago in range(days_back):
                snapshot_date = current_date - pd.Timedelta(days=days_ago)
                
                # Filter videos that existed on this date
                df = self.video_stats.copy()
                df['publish_date'] = pd.to_datetime(df['publish_date'])
                existed_videos = df[df['publish_date'] <= snapshot_date]
                
                if not existed_videos.empty:
                    # Calculate metrics as of this date
                    video_ids = existed_videos['video_id'].tolist()
                    factory_kpis = self._aggregate_from_video_ids(video_ids, snapshot_date)
                    
                    snapshots.append({
                        'date': snapshot_date.isoformat(),
                        'total_videos': factory_kpis.total_videos,
                        'total_views': factory_kpis.total_views,
                        'baseline_compliance': factory_kpis.baseline_compliance_rate,
                        'avg_engagement': factory_kpis.avg_engagement_rate,
                        'underperforming_count': factory_kpis.underperforming_count
                    })
            
            return snapshots
            
        except Exception as e:
            print(f"[FactoryMetrics] Error getting historical snapshots: {e}")
            return []
    
    def _aggregate_from_video_ids(self, video_ids: List[str], as_of_date: datetime) -> FactoryKPIs:
        """Helper method to aggregate from specific video IDs as of a date."""
        try:
            video_kpis = []
            
            for video_id in video_ids:
                kpi = self.compute_per_video_metrics(video_id)
                if kpi and kpi.timestamp <= as_of_date:
                    video_kpis.append(kpi)
            
            if not video_kpis:
                return self._empty_factory_kpis()
            
            total_videos = len(video_kpis)
            total_views = sum(k.total_views for k in video_kpis)
            avg_views = total_views / total_videos
            
            avg_engagement = np.mean([k.engagement_rate for k in video_kpis])
            avg_velocity = np.mean([k.growth_velocity for k in video_kpis])
            avg_virality = np.mean([k.virality_score for k in video_kpis])
            
            videos_above_5m = sum(1 for k in video_kpis if k.total_views >= 5_000_000)
            baseline_compliance_rate = (videos_above_5m / total_videos) * 100
            underperforming = sum(1 for k in video_kpis if k.needs_intervention)
            
            return FactoryKPIs(
                niche=self.niche,
                timestamp=as_of_date,
                total_videos=total_videos,
                total_views=total_views,
                avg_views_per_video=round(avg_views, 2),
                avg_engagement_rate=round(avg_engagement, 4),
                baseline_compliance_rate=round(baseline_compliance_rate, 2),
                videos_above_5m=videos_above_5m,
                videos_above_10m=sum(1 for k in video_kpis if k.total_views >= 10_000_000),
                videos_above_30m=sum(1 for k in video_kpis if k.total_views >= 30_000_000),
                avg_growth_velocity=round(avg_velocity, 2),
                avg_virality_score=round(avg_virality, 2),
                long_tail_potential=0.0,  # Simplified for historical
                underperforming_count=underperforming,
                daily_upload_rate=0.0  # Simplified for historical
            )
            
        except Exception as e:
            print(f"[FactoryMetrics] Error aggregating from video IDs: {e}")
            return self._empty_factory_kpis()
    
    def get_orchestration_signals(self) -> Dict:
        """
        Provide structured output contracts for downstream orchestrators.
        
        Returns:
            Dict: Orchestration-ready signals with priority queue and action taxonomy
        """
        try:
            factory_kpis = self.aggregate_factory_metrics()
            underperforming = self.identify_underperforming_videos()
            
            # Priority queue with action taxonomy
            priority_queue = {
                'CRITICAL': [],    # Immediate action required (<24 hours)
                'HIGH': [],        # Action within 48 hours
                'MEDIUM': [],      # Action within 7 days
                'LOW': []          # Monitor, action within 30 days
            }
            
            action_taxonomy = {
                'boosters': {
                    'paid_immediate': [],
                    'paid_standard': [],
                    'organic_cross_promo': [],
                    'repost_optimized': []
                },
                'content': {
                    'thumbnail_test': [],
                    'title_optimize': [],
                    'description_hooks': [],
                    'reedit_pacing': []
                },
                'strategy': {
                    'topic_frequency': [],
                    'posting_schedule': [],
                    'content_repurpose': [],
                    'archive_content': []
                }
            }
            
            # Classify underperforming videos
            for video in underperforming:
                severity = video['severity']
                actions = video['recommended_actions']
                
                # Determine priority
                if severity >= 80:
                    priority = 'CRITICAL'
                elif severity >= 60:
                    priority = 'HIGH'
                elif severity >= 40:
                    priority = 'MEDIUM'
                else:
                    priority = 'LOW'
                
                # Add to priority queue
                priority_queue[priority].append({
                    'video_id': video['video_id'],
                    'severity': severity,
                    'gap_to_baseline': video['gap_to_baseline'],
                    'days_live': video['days_live'],
                    'recommended_actions': actions,
                    'estimated_cost': self._estimate_action_cost(actions),
                    'expected_impact': self._estimate_action_impact(video, actions),
                    'cooldown_until': self._calculate_cooldown(video['video_id'])
                })
                
                # Classify actions by taxonomy
                for action in actions:
                    if 'paid' in action.lower():
                        if 'immediately' in action.lower():
                            action_taxonomy['boosters']['paid_immediate'].append(video['video_id'])
                        else:
                            action_taxonomy['boosters']['paid_standard'].append(video['video_id'])
                    elif 'cross-promote' in action.lower():
                        action_taxonomy['boosters']['organic_cross_promo'].append(video['video_id'])
                    elif 'repost' in action.lower():
                        action_taxonomy['boosters']['repost_optimized'].append(video['video_id'])
                    elif 'thumbnail' in action.lower():
                        action_taxonomy['content']['thumbnail_test'].append(video['video_id'])
                    elif 'title' in action.lower():
                        action_taxonomy['content']['title_optimize'].append(video['video_id'])
                    elif 'hooks' in action.lower():
                        action_taxonomy['content']['description_hooks'].append(video['video_id'])
                    elif 're-edit' in action.lower() or 'pacing' in action.lower():
                        action_taxonomy['content']['reedit_pacing'].append(video['video_id'])
                    elif 'frequency' in action.lower():
                        action_taxonomy['strategy']['topic_frequency'].append(video['video_id'])
                    elif 'archiv' in action.lower():
                        action_taxonomy['strategy']['archive_content'].append(video['video_id'])
                    else:
                        action_taxonomy['strategy']['posting_schedule'].append(video['video_id'])
            
            # Global severity ranking
            all_videos = []
            for priority, videos in priority_queue.items():
                for video in videos:
                    video['priority'] = priority
                    all_videos.append(video)
            
            all_videos.sort(key=lambda x: (x['severity'], x['gap_to_baseline']), reverse=True)
            
            return {
                'factory_kpis': asdict(factory_kpis),
                'priority_queue': priority_queue,
                'action_taxonomy': action_taxonomy,
                'global_ranking': all_videos,
                'orchestration_metadata': {
                    'total_actions_needed': len(all_videos),
                    'critical_actions': len(priority_queue['CRITICAL']),
                    'estimated_total_cost': sum(v['estimated_cost'] for v in all_videos),
                    'generated_at': datetime.now().isoformat(),
                    'next_review': (datetime.now() + pd.Timedelta(hours=24)).isoformat()
                }
            }
            
        except Exception as e:
            print(f"[FactoryMetrics] Error generating orchestration signals: {e}")
            return {}
    
    def _estimate_action_cost(self, actions: List[str]) -> float:
        """Estimate cost of recommended actions (0-100 scale)."""
        cost = 0.0
        
        for action in actions:
            if 'paid' in action.lower():
                cost += 40.0
            elif 'repost' in action.lower():
                cost += 20.0
            elif 're-edit' in action.lower():
                cost += 30.0
            elif 'test' in action.lower():
                cost += 15.0
            else:
                cost += 10.0
        
        return min(100.0, cost)
    
    def _estimate_action_impact(self, video: Dict, actions: List[str]) -> float:
        """Estimate expected impact of actions (0-100 scale)."""
        base_impact = 50.0
        
        # Higher impact for larger gaps
        gap_ratio = video['gap_to_baseline'] / 5_000_000
        gap_bonus = min(30.0, gap_ratio * 30.0)
        
        # Higher impact for newer videos
        if video['days_live'] < 14:
            age_bonus = 20.0
        elif video['days_live'] < 30:
            age_bonus = 10.0
        else:
            age_bonus = 0.0
        
        return min(100.0, base_impact + gap_bonus + age_bonus)
    
    def _calculate_cooldown(self, video_id: str) -> Optional[str]:
        """Calculate cooldown period for actions."""
        # Simple cooldown logic - could be enhanced with actual action history
        cooldown_hours = 24
        cooldown_until = datetime.now() + pd.Timedelta(hours=cooldown_hours)
        return cooldown_until.isoformat()
    
    def push_to_dashboard_api(self, dashboard_api_url: str) -> bool:
        """
        Actual API client integration for dashboard metrics.
        
        Args:
            dashboard_api_url: Dashboard API endpoint URL
        
        Returns:
            bool: Success status
        """
        try:
            import requests
            
            factory_kpis = self.aggregate_factory_metrics()
            underperforming = self.identify_underperforming_videos()
            orchestration_signals = self.get_orchestration_signals()
            
            payload = {
                'schema_version': '1.0',
                'factory_kpis': asdict(factory_kpis),
                'underperforming_videos': underperforming,
                'orchestration_signals': orchestration_signals,
                'temporal_aggregates': {
                    'daily': self.get_temporal_aggregates('daily', 7),
                    'weekly': self.get_temporal_aggregates('weekly', 4),
                    'monthly': self.get_temporal_aggregates('monthly', 3)
                },
                'metadata': {
                    'timestamp': datetime.now().isoformat(),
                    'niche': self.niche,
                    'processing_version': '2.0'
                }
            }
            
            response = requests.post(
                dashboard_api_url,
                json=payload,
                headers={'Content-Type': 'application/json'},
                timeout=30
            )
            
            if response.status_code == 200:
                print(f"[FactoryMetrics] Successfully pushed to dashboard API")
                return True
            else:
                print(f"[FactoryMetrics] API error: {response.status_code} - {response.text}")
                return False
                
        except ImportError:
            print("[FactoryMetrics] requests library not available, using file fallback")
            self.push_metrics_to_dashboard()
            return False
        except Exception as e:
            print(f"[FactoryMetrics] Error pushing to API: {e}")
            return False
    
    def enforce_baseline_sla(self) -> Dict:
        """
        Deterministic enforcement loop for baseline achievement with SLA tracking.
        
        Returns:
            Dict: SLA enforcement status and actions taken
        """
        try:
            factory_kpis = self.aggregate_factory_metrics()
            underperforming = self.identify_underperforming_videos()
            
            sla_status = {
                'baseline_compliance': factory_kpis.baseline_compliance_rate,
                'sla_target': 80.0,  # 80% baseline compliance target
                'sla_met': factory_kpis.baseline_compliance_rate >= 80.0,
                'underperforming_count': factory_kpis.underperforming_count,
                'total_videos': factory_kpis.total_videos,
                'enforcement_actions': [],
                'escalation_tier': self._calculate_escalation_tier(factory_kpis),
                'time_to_baseline_projections': {}
            }
            
            # Calculate time-to-baseline projections
            for video in underperforming[:10]:  # Top 10 for efficiency
                projection = self._calculate_time_to_baseline(video)
                sla_status['time_to_baseline_projections'][video['video_id']] = projection
            
            # Determine enforcement actions
            if not sla_status['sla_met']:
                escalation_tier = sla_status['escalation_tier']
                
                if escalation_tier == 'CRITICAL':
                    sla_status['enforcement_actions'].extend([
                        'IMMEDIATE: Allocate emergency booster budget',
                        'IMMEDIATE: Pause new content publishing',
                        'IMMEDIATE: Escalate to content strategy team'
                    ])
                elif escalation_tier == 'HIGH':
                    sla_status['enforcement_actions'].extend([
                        'WITHIN 24H: Increase booster allocation by 50%',
                        'WITHIN 24H: Optimize posting schedule',
                        'WITHIN 48H: Review content quality standards'
                    ])
                elif escalation_tier == 'MEDIUM':
                    sla_status['enforcement_actions'].extend([
                        'WITHIN 72H: Apply targeted boosters',
                        'WITHIN 72H: A/B test thumbnails/titles',
                        'WITHIN 7 DAYS: Content strategy review'
                    ])
                else:
                    sla_status['enforcement_actions'].extend([
                        'WITHIN 7 DAYS: Monitor and optimize',
                        'WITHIN 14 DAYS: Apply organic boosters'
                    ])
            
            # Log SLA status
            self._log_sla_status(sla_status)
            
            return sla_status
            
        except Exception as e:
            print(f"[FactoryMetrics] Error enforcing baseline SLA: {e}")
            return {}
    
    def _calculate_escalation_tier(self, factory_kpis: FactoryKPIs) -> str:
        """Calculate escalation tier based on factory performance."""
        compliance_rate = factory_kpis.baseline_compliance_rate
        underperforming_ratio = factory_kpis.underperforming_count / max(1, factory_kpis.total_videos)
        
        if compliance_rate < 50 or underperforming_ratio > 0.5:
            return 'CRITICAL'
        elif compliance_rate < 65 or underperforming_ratio > 0.3:
            return 'HIGH'
        elif compliance_rate < 75 or underperforming_ratio > 0.2:
            return 'MEDIUM'
        else:
            return 'LOW'
    
    def _calculate_time_to_baseline(self, video: Dict) -> Dict:
        """Calculate time-to-baseline projection with confidence intervals."""
        try:
            current_views = video['current_views']
            gap_to_baseline = video['gap_to_baseline']
            growth_velocity = video['growth_velocity']
            days_live = video['days_live']
            
            # Simple projection with decay
            if growth_velocity > 0:
                # Account for velocity decay over time
                decay_factor = max(0.3, 1.0 - (days_live / 180))  # Decay over 6 months
                effective_velocity = growth_velocity * decay_factor
                
                days_to_baseline = gap_to_baseline / max(1, effective_velocity)
                
                # Add confidence intervals
                confidence_low = days_to_baseline * 0.7
                confidence_high = days_to_baseline * 1.5
                
                # Cap at reasonable values
                days_to_baseline = min(365, max(1, days_to_baseline))
                confidence_low = min(365, max(1, confidence_low))
                confidence_high = min(365, max(1, confidence_high))
                
                return {
                    'days_to_baseline': round(days_to_baseline, 1),
                    'confidence_interval': (round(confidence_low, 1), round(confidence_high, 1)),
                    'projection_method': 'velocity_decay',
                    'meets_sla': days_to_baseline <= 30  # 30-day SLA
                }
            else:
                return {
                    'days_to_baseline': float('inf'),
                    'confidence_interval': (float('inf'), float('inf')),
                    'projection_method': 'no_growth',
                    'meets_sla': False
                }
        
        except Exception as e:
            print(f"[FactoryMetrics] Error calculating time to baseline: {e}")
            return {
                'days_to_baseline': float('inf'),
                'confidence_interval': (float('inf'), float('inf')),
                'projection_method': 'error',
                'meets_sla': False
            }
    
    def _log_sla_status(self, sla_status: Dict):
        # Log SLA status to file or database
        pass
    
    def generate_rl_state_vector(self, kpis: VideoKPIs) -> RLStateVector:
        """Generate RL state vector with multiple features."""
        try:
            # Normalize virality score (0-100 -> 0-1)
            virality_normalized = min(1.0, kpis.virality_score / 100.0)
            
            # Normalize baseline gap ratio
            baseline_gap = max(0, self.baseline_views - kpis.total_views)
            baseline_gap_ratio = min(1.0, baseline_gap / self.baseline_views)
            
            # Normalize growth velocity
            expected_daily_velocity = self.baseline_views / 30
            velocity_normalized = min(1.0, kpis.growth_velocity / expected_daily_velocity)
            
            # Normalize engagement rate
            engagement_normalized = min(1.0, kpis.engagement_rate / (self.target_engagement_rate * 2))
            
            # Normalize retention rate
            retention_normalized = min(1.0, kpis.avg_retention_rate / 1.0)
            
            # Normalize days live (0-90 days -> 0-1)
            days_live_normalized = min(1.0, kpis.days_since_publish / 90.0)
            
            # Get long-tail score
            if LONG_TAIL_AVAILABLE and self.long_tail_tracker:
                try:
                    long_tail_score = self.long_tail_tracker.compute_long_tail_score(kpis.video_id)
                except:
                    long_tail_score = 0.5
            else:
                long_tail_score = 0.5
            long_tail_normalized = min(1.0, long_tail_score)
            
            # Get ML confidence score
            prediction = self._ml_predictions_cache.get(kpis.video_id)
            if isinstance(prediction, MLPrediction):
                confidence_normalized = prediction.probability_baseline
            else:
                confidence_normalized = 0.5
            
            return RLStateVector(
                virality_score_normalized=virality_normalized,
                baseline_gap_ratio=baseline_gap_ratio,
                growth_velocity_normalized=velocity_normalized,
                engagement_rate_normalized=engagement_normalized,
                retention_rate_normalized=retention_normalized,
                days_live_normalized=days_live_normalized,
                long_tail_score_normalized=long_tail_normalized,
                confidence_score_normalized=confidence_normalized
            )
            
        except Exception as e:
            print(f"[FactoryMetrics] Error generating RL state vector: {e}")
            return RLStateVector(0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5)
    
    def generate_rl_reward_signal(
        self, 
        video_id: str, 
        action_taken: Optional[str] = None,
        previous_kpis: Optional[VideoKPIs] = None
    ) -> RLRewardSignal:
        """Generate RL reward signal with multiple components for reward shaping."""
        try:
            current_kpis = self.compute_per_video_metrics(video_id)
            if not current_kpis:
                raise ValueError(f"Could not compute KPIs for video {video_id}")
            
            state_vector = self.generate_rl_state_vector(current_kpis)
            
            # Compute reward components
            reward_components = {}
            
            # 1. Baseline achievement reward
            if current_kpis.meets_baseline:
                reward_components['baseline_achievement'] = 100.0
            else:
                progress = current_kpis.total_views / self.baseline_views
                reward_components['baseline_achievement'] = progress * 50.0
            
            # 2. Growth velocity reward
            expected_velocity = self.baseline_views / 30
            velocity_ratio = min(2.0, current_kpis.growth_velocity / expected_velocity)
            reward_components['growth_velocity'] = velocity_ratio * 20.0
            
            # 3. Engagement reward
            engagement_ratio = min(2.0, current_kpis.engagement_rate / self.target_engagement_rate)
            reward_components['engagement'] = engagement_ratio * 15.0
            
            # 4. Long-tail potential reward
            if LONG_TAIL_AVAILABLE and self.long_tail_tracker:
                try:
                    long_tail_score = self.long_tail_tracker.compute_long_tail_score(video_id)
                    reward_components['long_tail'] = long_tail_score * 15.0
                except Exception:
                    reward_components['long_tail'] = 0.0
            else:
                reward_components['long_tail'] = 0.0
            
            # 5. Improvement reward (if previous state available)
            if previous_kpis:
                view_improvement = (current_kpis.total_views - previous_kpis.total_views) / max(1, previous_kpis.total_views)
                reward_components['improvement'] = min(20.0, view_improvement * 100)
            else:
                reward_components['improvement'] = 0.0
            
            # 6. Time efficiency penalty
            time_penalty = max(0, (current_kpis.days_since_publish - 30) / 60)
            reward_components['time_penalty'] = -time_penalty * 10.0
            
            # Calculate total reward and normalize to [-1, 1]
            total_reward = sum(reward_components.values())
            total_reward = max(-1.0, min(1.0, total_reward / 100.0))
            
            # Generate action state if action was taken
            action_state = None
            feedback_signal = None
            
            if action_taken and previous_kpis:
                previous_state = self.generate_rl_state_vector(previous_kpis)
                
                # Calculate action effectiveness
                view_delta = current_kpis.total_views - previous_kpis.total_views
                expected_improvement = self._get_expected_improvement(action_taken)
                action_effectiveness = min(1.0, view_delta / max(1, expected_improvement))
                
                # Calculate cost-benefit ratio
                action_cost = self._get_action_cost(action_taken)
                benefit = view_delta * 0.001  # Assume $0.001 per view
                cost_benefit_ratio = benefit / max(0.01, action_cost)
                
                # Calculate urgency score
                urgency = self._calculate_intervention_severity(current_kpis) / 100.0
                
                feedback_signal = RLFeedbackSignal(
                    action_effectiveness=action_effectiveness,
                    cost_benefit_ratio=cost_benefit_ratio,
                    urgency_score=urgency,
                    intervention_type=action_taken,
                    expected_improvement=expected_improvement
                )
                
                action_state = RLActionState(
                    video_id=video_id,
                    state_vector=previous_state,
                    action_taken=action_taken,
                    reward=total_reward,
                    feedback_signal=feedback_signal,
                    next_state_vector=state_vector,
                    timestamp=datetime.now()
                )
            
            # Calculate confidence interval
            prediction = self._ml_predictions_cache.get(video_id)
            if isinstance(prediction, MLPrediction):
                confidence_low = prediction.confidence_low / self.baseline_views
                confidence_high = prediction.confidence_high / self.baseline_views
            else:
                confidence_low = 0.8
                confidence_high = 1.2
            
            return RLRewardSignal(
                video_id=video_id,
                reward=total_reward,
                reward_components=reward_components,
                state_vector=state_vector,
                action_state=action_state,
                feedback_signal=feedback_signal,
                confidence_interval=(confidence_low, confidence_high),
                model_version=self.ml_predictor.get_model_info()['model_version'],
                timestamp=datetime.now()
            )
            
        except Exception as e:
            print(f"[FactoryMetrics] Error generating RL reward signal: {e}")
            return RLRewardSignal(
                video_id=video_id,
                reward=0.0,
                reward_components={},
                state_vector=RLStateVector(0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5),
                action_state=None,
                feedback_signal=None,
                confidence_interval=(0.0, 0.0),
                model_version="error",
                timestamp=datetime.now()
            )
    
    def _get_expected_improvement(self, action: str) -> float:
        """Get expected view improvement for an action type."""
        improvements = {
            'immediate_boost': 50000,
            'boost': 25000,
            'repost': 15000,
            'optimize': 10000,
            'thumbnail_test': 8000,
            'title_optimize': 5000
        }
        return improvements.get(action, 5000)
    
    def _get_action_cost(self, action: str) -> float:
        """Get cost for an action type."""
        costs = {
            'immediate_boost': 100.0,
            'boost': 50.0,
            'repost': 20.0,
            'optimize': 30.0,
            'thumbnail_test': 15.0,
            'title_optimize': 10.0
        }
        return costs.get(action, 25.0)
    
    def _calculate_intervention_severity(self, kpis: VideoKPIs) -> float:
        """Calculate intervention severity score (0-100)."""
        severity = 0.0
        
        # Baseline check
        if not kpis.meets_baseline:
            gap_ratio = 1.0 - (kpis.total_views / self.baseline_views)
            severity += gap_ratio * 40.0
        
        # Engagement check
        if kpis.engagement_rate < self.target_engagement_rate:
            engagement_gap = 1.0 - (kpis.engagement_rate / self.target_engagement_rate)
            severity += engagement_gap * 25.0
        
        # Growth velocity check
        expected_velocity = self.baseline_views / 30
        if kpis.growth_velocity < expected_velocity:
            velocity_gap = 1.0 - (kpis.growth_velocity / expected_velocity)
            severity += velocity_gap * 25.0
        
        # Time-based check for older videos
        if kpis.days_since_publish > 30:
            time_urgency = min(1.0, kpis.days_since_publish / 90)
            severity += time_urgency * 10.0
        
        return min(100.0, severity)
    
    async def async_batch_compute_metrics(self, video_ids: Optional[List[str]] = None) -> BatchMetricsResult:
        """Async version of batch compute metrics for concurrent processing."""
        try:
            start_time = datetime.now()
            
            if video_ids is None:
                video_ids = self.video_stats['video_id'].unique().tolist()
            
            # Filter valid videos
            valid_videos = [vid for vid in video_ids if vid in self.video_stats['video_id'].values]
            
            if not valid_videos:
                return BatchMetricsResult(
                    video_kpis=[],
                    processing_time_ms=0.0,
                    videos_processed=0,
                    throughput_per_second=0.0,
                    batch_size=len(video_ids) if video_ids else 0,
                    ml_prediction_time_ms=0.0,
                    long_tail_time_ms=0.0
                )
            
            # Process in smaller concurrent chunks
            chunk_size = min(100, len(valid_videos))
            chunks = [valid_videos[i:i + chunk_size] for i in range(0, len(valid_videos), chunk_size)]
            
            semaphore = asyncio.Semaphore(self.max_concurrent_batches)
            
            async def process_chunk(chunk):
                async with semaphore:
                    # Load ML predictions for chunk
                    ml_start = datetime.now()
                    await self._load_ml_predictions_async(chunk)
                    ml_time = (datetime.now() - ml_start).total_seconds() * 1000
                    
                    # Compute KPIs for chunk
                    chunk_kpis = []
                    for video_id in chunk:
                        kpi = self.compute_per_video_metrics(video_id)
                        if kpi:
                            chunk_kpis.append(kpi)
                    
                    return chunk_kpis, ml_time
            
            # Process all chunks concurrently
            chunk_results = await asyncio.gather(*[process_chunk(chunk) for chunk in chunks])
            
            # Combine results
            all_kpis = []
            total_ml_time = 0.0
            for chunk_kpis, ml_time in chunk_results:
                all_kpis.extend(chunk_kpis)
                total_ml_time += ml_time
            
            processing_time_ms = (datetime.now() - start_time).total_seconds() * 1000
            throughput = len(all_kpis) / (processing_time_ms / 1000) if processing_time_ms > 0 else 0
            
            return BatchMetricsResult(
                video_kpis=all_kpis,
                processing_time_ms=processing_time_ms,
                videos_processed=len(all_kpis),
                throughput_per_second=throughput,
                batch_size=len(video_ids) if video_ids else 0,
                ml_prediction_time_ms=total_ml_time,
                long_tail_time_ms=0.0  # Could be measured if needed
            )
            
        except Exception as e:
            print(f"[FactoryMetrics] Error in async batch compute: {e}")
            return BatchMetricsResult(
                video_kpis=[],
                processing_time_ms=0.0,
                videos_processed=0,
                throughput_per_second=0.0,
                batch_size=len(video_ids) if video_ids else 0,
                ml_prediction_time_ms=0.0,
                long_tail_time_ms=0.0
            )
    
    def vectorized_batch_compute_metrics(self, video_ids: Optional[List[str]] = None) -> BatchMetricsResult:
        """TRUE vectorized batch processing using Pandas operations for 50k-100k videos/day scaling."""
        try:
            start_time = datetime.now()
            
            if video_ids is None:
                video_ids = self.video_stats['video_id'].unique().tolist()
            
            # Filter and prepare batch data
            batch_data = self.video_stats[self.video_stats['video_id'].isin(video_ids)].copy()
            
            if batch_data.empty:
                return BatchMetricsResult(
                    video_kpis=[],
                    processing_time_ms=0.0,
                    videos_processed=0,
                    throughput_per_second=0.0,
                    batch_size=len(video_ids) if video_ids else 0,
                    ml_prediction_time_ms=0.0,
                    long_tail_time_ms=0.0
                )
            
            # Get latest record for each video (vectorized)
            latest_indices = batch_data.groupby('video_id')['timestamp'].idxmax()
            batch_data = batch_data.loc[latest_indices]
            
            # VECTORIZED STEP 1: Calculate engagement rates
            def vectorized_engagement_rate(row):
                weights = self.platform_weights.get(row['platform'].lower(), 
                                                   self.platform_weights['youtube'])
                weighted_engagement = (
                    row['likes'] * weights['likes'] +
                    row['comments'] * weights['comments'] +
                    row['shares'] * weights['shares']
                )
                return weighted_engagement / row['views'] if row['views'] > 0 else 0.0
            
            batch_data['engagement_rate'] = batch_data.apply(vectorized_engagement_rate, axis=1)
            
            # VECTORIZED STEP 2: Calculate growth velocities
            now = datetime.now()
            batch_data['days_live'] = (now - batch_data['publish_date']).dt.days.clip(lower=1)
            batch_data['growth_velocity'] = batch_data['views'] / batch_data['days_live']
            
            # VECTORIZED STEP 3: Calculate retention rates (ensure column exists)
            if 'retention_rate' not in batch_data.columns:
                batch_data['retention_rate'] = 0.5
            
            # VECTORIZED STEP 4: Calculate virality scores
            def vectorized_virality_score(row):
                view_score = min(40, (row['views'] / self.baseline_views) * 20)
                engagement_score = min(20, (row['engagement_rate'] / self.target_engagement_rate) * 20)
                expected_daily_views = self.baseline_views / 30
                velocity_score = min(30, (row['growth_velocity'] / expected_daily_views) * 30)
                retention_score = min(20, (row['retention_rate'] / self.min_retention_rate) * 20)
                
                time_factor = 1.0
                if row['days_live'] < 7:
                    time_factor = 1.2
                elif row['days_live'] > 60:
                    time_factor = 0.8
                
                return (view_score + engagement_score + velocity_score + retention_score) * time_factor
            
            batch_data['virality_score'] = batch_data.apply(vectorized_virality_score, axis=1)
            
            # VECTORIZED STEP 5: Load ML predictions for batch
            ml_start = datetime.now()
            video_ids_list = batch_data['video_id'].tolist()
            self._load_ml_predictions(video_ids_list)
            ml_time = (datetime.now() - ml_start).total_seconds() * 1000
            
            # VECTORIZED STEP 6: Create VideoKPIs objects
            video_kpis = []
            for _, row in batch_data.iterrows():
                kpi = VideoKPIs(
                    video_id=row['video_id'],
                    timestamp=row['timestamp'],
                    total_views=int(row['views']),
                    likes=int(row['likes']),
                    comments=int(row['comments']),
                    shares=int(row['shares']),
                    watch_time_hours=float(row.get('watch_time_hours', row['views'] * 0.05)),
                    avg_retention_rate=float(row['retention_rate']),
                    engagement_rate=float(row['engagement_rate']),
                    growth_velocity=float(row['growth_velocity']),
                    virality_score=float(row['virality_score']),
                    days_since_publish=int(row['days_live']),
                    meets_baseline=row['views'] >= self.baseline_views,
                    needs_intervention=self._needs_intervention(row),
                    projected_final_views=self._get_projected_views(row['video_id'])
                )
                video_kpis.append(kpi)
            
            processing_time_ms = (datetime.now() - start_time).total_seconds() * 1000
            throughput = len(video_kpis) / (processing_time_ms / 1000) if processing_time_ms > 0 else 0
            
            return BatchMetricsResult(
                video_kpis=video_kpis,
                processing_time_ms=processing_time_ms,
                videos_processed=len(video_kpis),
                throughput_per_second=throughput,
                batch_size=len(video_ids) if video_ids else 0,
                ml_prediction_time_ms=ml_time,
                long_tail_time_ms=0.0
            )
            
        except Exception as e:
            print(f"[FactoryMetrics] Error in vectorized batch compute: {e}")
            return BatchMetricsResult(
                video_kpis=[],
                processing_time_ms=0.0,
                videos_processed=0,
                throughput_per_second=0.0,
                batch_size=len(video_ids) if video_ids else 0,
                ml_prediction_time_ms=0.0,
                long_tail_time_ms=0.0
            )
    
    def _needs_intervention(self, row) -> bool:
        """Determine if a video needs intervention based on multiple factors."""
        # Baseline check
        if row['views'] < self.baseline_views * 0.3:  # Less than 30% of baseline
            return True
        
        # Engagement check
        if row['engagement_rate'] < self.target_engagement_rate * 0.5:  # Less than 50% of target
            return True
        
        # Growth velocity check
        expected_velocity = self.baseline_views / 30
        if row['growth_velocity'] < expected_velocity * 0.3:  # Less than 30% of expected
            return True
        
        # Time-based check for older videos
        if row['days_live'] > 30 and row['views'] < self.baseline_views * 0.7:
            return True
        
        return False
    
    def _get_projected_views(self, video_id: str) -> int:
        """Get projected final views from ML predictions."""
        prediction = self._ml_predictions_cache.get(video_id)
        if isinstance(prediction, MLPrediction):
            return prediction.predicted_views
        else:
            # Fallback: simple extrapolation
            try:
                video_data = self.video_stats[self.video_stats['video_id'] == video_id]
                if not video_data.empty:
                    current_views = video_data.iloc[0]['views']
                    days_live = max(1, (datetime.now() - video_data.iloc[0]['publish_date']).days)
                    daily_velocity = current_views / days_live
                    projected = int(current_views + (daily_velocity * max(0, 90 - days_live) * 0.7))
                    return projected
            except:
                pass
        return 0
    
    def get_confidence_adjusted_baseline(self, video_id: str) -> Dict:
        """
        Calculate confidence-adjusted baseline logic with probability-based enforcement.
        
        Args:
            video_id: ID of video to analyze
        
        Returns:
            Dict: Confidence-adjusted baseline information
        """
        try:
            kpi = self.compute_per_video_metrics(video_id)
            if not kpi:
                return {'error': 'Video not found'}
            
            # Calculate uncertainty based on various factors
            uncertainty = 0.1  # Base uncertainty
            
            # Higher uncertainty for new videos
            if kpi.days_since_publish < 7:
                uncertainty += 0.3
            elif kpi.days_since_publish < 30:
                uncertainty += 0.1
            
            # Higher uncertainty for low engagement
            if kpi.engagement_rate < 0.02:
                uncertainty += 0.2
            elif kpi.engagement_rate < 0.05:
                uncertainty += 0.1
            
            # Higher uncertainty for inconsistent growth
            if kpi.growth_velocity < 100:
                uncertainty += 0.2
            
            return {'confidence': min(1.0, uncertainty)}
                
        except Exception as e:
            print(f"[FactoryMetrics] Error calculating prediction uncertainty: {e}")
            return {'confidence': 0.5}
    
    def preserve_growth_curves(self) -> pd.DataFrame:
        """
        Preserve growth curves instead of dropping duplicates for better temporal fidelity.
        
        Returns:
            pd.DataFrame: Video stats with full temporal history preserved
        """
        try:
            if self.video_stats.empty:
                return pd.DataFrame()
            
            # Keep all historical data, don't drop duplicates
            df = self.video_stats.copy()
            
            # Sort by video_id and timestamp to maintain chronological order
            df = df.sort_values(['video_id', 'timestamp'])
            
            # Add temporal features
            df['days_since_publish'] = df.apply(
                lambda row: (pd.to_datetime(row['timestamp']) - pd.to_datetime(row['publish_date'])).days,
                axis=1
            )
            
            # Calculate rolling metrics for each video
            df['rolling_views_7d'] = df.groupby('video_id')['views'].transform(
                lambda x: x.rolling(window=7, min_periods=1).mean()
            )
            
            df['rolling_engagement_7d'] = df.groupby('video_id').apply(
                lambda group: group.sort_values('timestamp')['engagement_rate'].rolling(window=7, min_periods=1).mean()
            ).reset_index(level=0, drop=True)
            
            # Calculate acceleration (second derivative)
            df['velocity_acceleration'] = df.groupby('video_id')['views'].transform(
                lambda x: x.diff().diff()  # Second derivative of views over time
            )
            
            print(f"[FactoryMetrics] Preserved growth curves for {len(df)} records")
            
            return df
        except Exception as e:
            print(f"[FactoryMetrics] Error preserving growth curves: {e}")
            return pd.DataFrame()

    def track_acceleration_metrics(self, video_id: str) -> Dict:
        """
        Second-Derivative Acceleration Tracking.
        
        Track: Velocity change, Acceleration (Δ velocity), Jerk (Δ acceleration)
        Acceleration > velocity for early virality detection.
        """
        try:
            video_data = self.video_stats[self.video_stats['video_id'] == video_id].sort_values('timestamp')
            
            if len(video_data) < 3:
                return {'acceleration': 0, 'jerk': 0, 'velocity_trend': 'insufficient_data'}
            
            # Calculate velocity at each time point
            velocities = []
            timestamps = []
            
            for _, row in video_data.iterrows():
                days_since_publish = (pd.to_datetime(row['timestamp']) - pd.to_datetime(row['publish_date'])).days
                if days_since_publish > 0:
                    velocity = row['views'] / days_since_publish
                    velocities.append(velocity)
                    timestamps.append(days_since_publish)
            
            if len(velocities) < 3:
                return {'acceleration': 0, 'jerk': 0, 'velocity_trend': 'insufficient_data'}
            
            # Calculate acceleration (first derivative of velocity)
            accelerations = []
            for i in range(1, len(velocities)):
                dt = timestamps[i] - timestamps[i-1]
                if dt > 0:
                    dv = velocities[i] - velocities[i-1]
                    acceleration = dv / dt
                    accelerations.append(acceleration)
            
            # Calculate jerk (second derivative of velocity)
            jerks = []
            for i in range(1, len(accelerations)):
                dt = timestamps[i] - timestamps[i-1]
                if dt > 0:
                    da = accelerations[i] - accelerations[i-1]
                    jerk = da / dt
                    jerks.append(jerk)
            
            # Current values
            current_acceleration = accelerations[-1] if accelerations else 0
            current_jerk = jerks[-1] if jerks else 0
            
            # Determine velocity trend
            if current_acceleration > 100:
                velocity_trend = 'accelerating'
            elif current_acceleration < -100:
                velocity_trend = 'decelerating'
            else:
                velocity_trend = 'stable'
            
            # Early virality detection (acceleration > velocity)
            current_velocity = velocities[-1]
            early_virality_signal = current_acceleration > current_velocity and current_velocity > 1000
            
            return {
                'current_velocity': current_velocity,
                'acceleration': current_acceleration,
                'jerk': current_jerk,
                'velocity_trend': velocity_trend,
                'early_virality_signal': early_virality_signal,
                'acceleration_history': accelerations[-5:] if len(accelerations) >= 5 else accelerations,
                'jerk_history': jerks[-3:] if len(jerks) >= 3 else jerks
            }
        
        except Exception as e:
            print(f"[FactoryMetrics] Error tracking acceleration metrics: {e}")
            return {'acceleration': 0, 'jerk': 0, 'velocity_trend': 'error'}

# 🧠 6. Cross-Video Cannibalization Detection System

@dataclass
class CannibalizationSignal:
    """Signal indicating potential video cannibalization."""
    video_id: str
    cannibalization_type: str  # 'same_topic', 'engagement_dilution', 'velocity_suppression'
    severity: str  # 'low', 'medium', 'high', 'critical'
    confidence: float  # 0-1 confidence score
    detected_at: datetime
    competing_videos: List[str]  # Videos that may be cannibalized
    topic_similarity: float  # 0-1 similarity score
    engagement_overlap: float  # 0-1 engagement overlap
    velocity_impact: float  # Negative velocity impact
    recommended_action: str  # 'pause_posting', 'reschedule', 'monitor'
    pause_duration_hours: int  # Recommended pause duration
    risk_score: float  # Overall cannibalization risk score
    metadata: Dict[str, Any]


@dataclass
class CannibalizationThresholds:
    """Thresholds for cannibalization detection."""
    topic_similarity_threshold: float = 0.8  # High similarity triggers detection
    engagement_overlap_threshold: float = 0.6  # Engagement overlap threshold
    velocity_suppression_threshold: float = 0.3  # Velocity suppression threshold
    time_window_hours: int = 72  # Time window to check for cannibalization
    minimum_videos_for_analysis: int = 3  # Minimum videos needed for analysis
    high_risk_threshold: float = 0.8  # High risk score threshold
    critical_risk_threshold: float = 0.9  # Critical risk score threshold


@dataclass
class CannibalizationAlert:
    """Alert for cannibalization detection with actionable recommendations."""
    alert_id: str
    niche: str
    alert_type: str  # 'cannibalization_detected'
    severity: str
    message: str
    detected_at: datetime
    signals: List[CannibalizationSignal]
    recommendation: str
    pause_duration_hours: int
    affected_videos: List[str]
    risk_assessment: str
    next_review_date: datetime
    auto_triggered: bool = True


class CannibalizationDetector:
    """
    Advanced cross-video cannibalization detection system.
    
    Detects when factories compete with themselves by analyzing:
    - Topic similarity between recent videos
    - Engagement dilution patterns
    - Velocity suppression effects
    - Temporal clustering of similar content
    """
    
    def __init__(self, thresholds: CannibalizationThresholds = None):
        self.thresholds = thresholds or CannibalizationThresholds()
        self.logger = logging.getLogger('CannibalizationDetector')
        
        # Cannibalization detection history
        self.detection_history: List[CannibalizationSignal] = []
        self.alert_history: List[CannibalizationAlert] = []
        
        # Topic similarity cache
        self.topic_similarity_cache: Dict[str, Dict[str, float]] = {}
        
        # Engagement patterns tracking
        self.engagement_patterns: Dict[str, List[float]] = {}
        
        # Velocity impact tracking
        self.velocity_impacts: Dict[str, List[float]] = {}
        
    def detect_cannibalization(self, 
                              video_data: pd.DataFrame, 
                              video_id: str, 
                              niche: str) -> List[CannibalizationSignal]:
        """
        Detect cannibalization patterns for a specific video.
        
        Args:
            video_data: DataFrame with video metrics
            video_id: Target video ID
            niche: Niche identifier
            
        Returns:
            List[CannibalizationSignal]: Detected cannibalization signals
        """
        try:
            signals = []
            
            # Get recent videos for comparison
            recent_videos = self._get_recent_videos(video_data, video_id)
            
            if len(recent_videos) < self.thresholds.minimum_videos_for_analysis:
                return signals
            
            # Detect same-topic cannibalization
            topic_signal = self._detect_same_topic_cannibalization(video_id, recent_videos, video_data)
            if topic_signal:
                signals.append(topic_signal)
            
            # Detect engagement dilution
            engagement_signal = self._detect_engagement_dilution(video_id, recent_videos, video_data)
            if engagement_signal:
                signals.append(engagement_signal)
            
            # Detect velocity suppression
            velocity_signal = self._detect_velocity_suppression(video_id, recent_videos, video_data)
            if velocity_signal:
                signals.append(velocity_signal)
            
            # Store signals in history
            self.detection_history.extend(signals)
            
            # Update tracking data
            self._update_tracking_data(video_id, signals)
            
            self.logger.info(f"Cannibalization detection for {video_id}: {len(signals)} signals found")
            
            return signals
            
        except Exception as e:
            self.logger.error(f"Error detecting cannibalization for {video_id}: {e}")
            return []
    
    def _get_recent_videos(self, video_data: pd.DataFrame, video_id: str) -> pd.DataFrame:
        """Get recent videos within the analysis time window."""
        try:
            # Get target video publish date
            target_video = video_data[video_data['video_id'] == video_id]
            if target_video.empty:
                return pd.DataFrame()
            
            target_publish_date = pd.to_datetime(target_video['publish_date'].iloc[0])
            
            # Filter recent videos within time window
            cutoff_date = target_publish_date - timedelta(hours=self.thresholds.time_window_hours)
            
            recent_videos = video_data[
                (pd.to_datetime(video_data['publish_date']) >= cutoff_date) &
                (pd.to_datetime(video_data['publish_date']) <= target_publish_date) &
                (video_data['video_id'] != video_id)
            ].sort_values('publish_date', ascending=False)
            
            return recent_videos
            
        except Exception as e:
            self.logger.error(f"Error getting recent videos: {e}")
            return pd.DataFrame()
    
    def _detect_same_topic_cannibalization(self, 
                                          video_id: str, 
                                          recent_videos: pd.DataFrame, 
                                          video_data: pd.DataFrame) -> Optional[CannibalizationSignal]:
        """Detect cannibalization due to similar topics."""
        try:
            if recent_videos.empty:
                return None
            
            # Calculate topic similarity with recent videos
            competing_videos = []
            similarity_scores = []
            
            for _, recent_video in recent_videos.iterrows():
                similarity = self._calculate_topic_similarity(video_id, recent_video['video_id'], video_data)
                
                if similarity >= self.thresholds.topic_similarity_threshold:
                    competing_videos.append(recent_video['video_id'])
                    similarity_scores.append(similarity)
            
            if not competing_videos:
                return None
            
            # Calculate severity based on similarity and number of competitors
            avg_similarity = sum(similarity_scores) / len(similarity_scores)
            competitor_count = len(competing_videos)
            
            # Determine severity
            if avg_similarity >= 0.9 and competitor_count >= 3:
                severity = 'critical'
                confidence = min(1.0, avg_similarity)
                pause_duration = 48
            elif avg_similarity >= 0.85 and competitor_count >= 2:
                severity = 'high'
                confidence = min(0.9, avg_similarity)
                pause_duration = 24
            elif avg_similarity >= 0.8 and competitor_count >= 1:
                severity = 'medium'
                confidence = min(0.8, avg_similarity)
                pause_duration = 12
            else:
                severity = 'low'
                confidence = avg_similarity
                pause_duration = 6
            
            # Calculate risk score
            risk_score = (avg_similarity * 0.4) + (competitor_count / 5 * 0.3) + (confidence * 0.3)
            
            return CannibalizationSignal(
                video_id=video_id,
                cannibalization_type='same_topic',
                severity=severity,
                confidence=confidence,
                detected_at=datetime.now(),
                competing_videos=competing_videos,
                topic_similarity=avg_similarity,
                engagement_overlap=0.0,  # Calculated separately
                velocity_impact=0.0,  # Calculated separately
                recommended_action='pause_posting',
                pause_duration_hours=pause_duration,
                risk_score=risk_score,
                metadata={
                    'competitor_count': competitor_count,
                    'analysis_window_hours': self.thresholds.time_window_hours,
                    'similarity_threshold_used': self.thresholds.topic_similarity_threshold
                }
            )
            
        except Exception as e:
            self.logger.error(f"Error detecting same-topic cannibalization: {e}")
            return None
    
    def _detect_engagement_dilution(self, 
                                   video_id: str, 
                                   recent_videos: pd.DataFrame, 
                                   video_data: pd.DataFrame) -> Optional[CannibalizationSignal]:
        """Detect engagement dilution due to competing videos."""
        try:
            if recent_videos.empty:
                return None
            
            # Get engagement patterns for target video
            target_engagement = self._get_engagement_pattern(video_id, video_data)
            if not target_engagement:
                return None
            
            # Calculate engagement overlap with recent videos
            competing_videos = []
            overlap_scores = []
            
            for _, recent_video in recent_videos.iterrows():
                recent_engagement = self._get_engagement_pattern(recent_video['video_id'], video_data)
                
                if recent_engagement:
                    overlap = self._calculate_engagement_overlap(target_engagement, recent_engagement)
                    
                    if overlap >= self.thresholds.engagement_overlap_threshold:
                        competing_videos.append(recent_video['video_id'])
                        overlap_scores.append(overlap)
            
            if not competing_videos:
                return None
            
            # Calculate severity
            avg_overlap = sum(overlap_scores) / len(overlap_scores)
            competitor_count = len(competing_videos)
            
            # Determine severity based on overlap impact
            if avg_overlap >= 0.8 and competitor_count >= 2:
                severity = 'critical'
                confidence = min(1.0, avg_overlap)
                pause_duration = 36
            elif avg_overlap >= 0.7 and competitor_count >= 2:
                severity = 'high'
                confidence = min(0.9, avg_overlap)
                pause_duration = 24
            elif avg_overlap >= 0.6 and competitor_count >= 1:
                severity = 'medium'
                confidence = min(0.8, avg_overlap)
                pause_duration = 12
            else:
                severity = 'low'
                confidence = avg_overlap
                pause_duration = 6
            
            # Calculate risk score
            risk_score = (avg_overlap * 0.5) + (competitor_count / 5 * 0.3) + (confidence * 0.2)
            
            return CannibalizationSignal(
                video_id=video_id,
                cannibalization_type='engagement_dilution',
                severity=severity,
                confidence=confidence,
                detected_at=datetime.now(),
                competing_videos=competing_videos,
                topic_similarity=0.0,  # Not relevant for engagement dilution
                engagement_overlap=avg_overlap,
                velocity_impact=0.0,  # Calculated separately
                recommended_action='pause_posting',
                pause_duration_hours=pause_duration,
                risk_score=risk_score,
                metadata={
                    'competitor_count': competitor_count,
                    'engagement_overlap_threshold': self.thresholds.engagement_overlap_threshold,
                    'target_engagement_avg': sum(target_engagement) / len(target_engagement) if target_engagement else 0
                }
            )
            
        except Exception as e:
            self.logger.error(f"Error detecting engagement dilution: {e}")
            return None
    
    def _detect_velocity_suppression(self, 
                                   video_id: str, 
                                   recent_videos: pd.DataFrame, 
                                   video_data: pd.DataFrame) -> Optional[CannibalizationSignal]:
        """Detect velocity suppression due to competing videos."""
        try:
            if recent_videos.empty:
                return None
            
            # Get velocity pattern for target video
            target_velocity = self._get_velocity_pattern(video_id, video_data)
            if not target_velocity:
                return None
            
            # Calculate velocity impact from recent videos
            competing_videos = []
            suppression_scores = []
            
            for _, recent_video in recent_videos.iterrows():
                recent_velocity = self._get_velocity_pattern(recent_video['video_id'], video_data)
                
                if recent_velocity:
                    suppression = self._calculate_velocity_suppression(target_velocity, recent_velocity)
                    
                    if suppression >= self.thresholds.velocity_suppression_threshold:
                        competing_videos.append(recent_video['video_id'])
                        suppression_scores.append(suppression)
            
            if not competing_videos:
                return None
            
            # Calculate severity
            avg_suppression = sum(suppression_scores) / len(suppression_scores)
            competitor_count = len(competing_videos)
            
            # Determine severity based on suppression impact
            if avg_suppression >= 0.5 and competitor_count >= 2:
                severity = 'critical'
                confidence = min(1.0, avg_suppression)
                pause_duration = 48
            elif avg_suppression >= 0.4 and competitor_count >= 2:
                severity = 'high'
                confidence = min(0.9, avg_suppression)
                pause_duration = 24
            elif avg_suppression >= 0.3 and competitor_count >= 1:
                severity = 'medium'
                confidence = min(0.8, avg_suppression)
                pause_duration = 12
            else:
                severity = 'low'
                confidence = avg_suppression
                pause_duration = 6
            
            # Calculate risk score
            risk_score = (avg_suppression * 0.6) + (competitor_count / 5 * 0.2) + (confidence * 0.2)
            
            return CannibalizationSignal(
                video_id=video_id,
                cannibalization_type='velocity_suppression',
                severity=severity,
                confidence=confidence,
                detected_at=datetime.now(),
                competing_videos=competing_videos,
                topic_similarity=0.0,  # Not relevant for velocity suppression
                engagement_overlap=0.0,  # Not relevant for velocity suppression
                velocity_impact=avg_suppression,
                recommended_action='pause_posting',
                pause_duration_hours=pause_duration,
                risk_score=risk_score,
                metadata={
                    'competitor_count': competitor_count,
                    'velocity_suppression_threshold': self.thresholds.velocity_suppression_threshold,
                    'target_velocity_avg': sum(target_velocity) / len(target_velocity) if target_velocity else 0
                }
            )
            
        except Exception as e:
            self.logger.error(f"Error detecting velocity suppression: {e}")
            return None
    
    def _calculate_topic_similarity(self, video_id1: str, video_id2: str, video_data: pd.DataFrame) -> float:
        """Calculate topic similarity between two videos."""
        try:
            # Check cache first
            cache_key = f"{video_id1}_{video_id2}"
            if cache_key in self.topic_similarity_cache:
                return self.topic_similarity_cache[cache_key]
            
            # Get video data
            video1_data = video_data[video_data['video_id'] == video_id1]
            video2_data = video_data[video_data['video_id'] == video_id2]
            
            if video1_data.empty or video2_data.empty:
                return 0.0
            
            # Calculate similarity based on multiple factors
            # 1. Engagement rate similarity
            eng_rate1 = video1_data['likes'].iloc[-1] / max(1, video1_data['views'].iloc[-1])
            eng_rate2 = video2_data['likes'].iloc[-1] / max(1, video2_data['views'].iloc[-1])
            engagement_similarity = 1.0 - abs(eng_rate1 - eng_rate2) / max(eng_rate1, eng_rate2)
            
            # 2. Growth velocity similarity
            velocity1 = self._calculate_growth_velocity(video1_data)
            velocity2 = self._calculate_growth_velocity(video2_data)
            velocity_similarity = 1.0 - abs(velocity1 - velocity2) / max(velocity1, velocity2, 1)
            
            # 3. Performance level similarity
            perf1 = video1_data['views'].iloc[-1]
            perf2 = video2_data['views'].iloc[-1]
            performance_similarity = 1.0 - abs(perf1 - perf2) / max(perf1, perf2, 1)
            
            # Weighted combination
            similarity = (engagement_similarity * 0.4 + velocity_similarity * 0.3 + performance_similarity * 0.3)
            
            # Cache result
            self.topic_similarity_cache[cache_key] = similarity
            
            return similarity
            
        except Exception as e:
            self.logger.error(f"Error calculating topic similarity: {e}")
            return 0.0
    
    def _calculate_growth_velocity(self, video_data: pd.DataFrame) -> float:
        """Calculate growth velocity for a video."""
        try:
            if len(video_data) < 2:
                return 0.0
            
            # Sort by timestamp
            sorted_data = video_data.sort_values('timestamp')
            
            # Calculate velocity as views per day
            first_views = sorted_data['views'].iloc[0]
            last_views = sorted_data['views'].iloc[-1]
            
            time_diff = (pd.to_datetime(sorted_data['timestamp'].iloc[-1]) - 
                        pd.to_datetime(sorted_data['timestamp'].iloc[0])).total_seconds() / 86400
            
            if time_diff <= 0:
                return 0.0
            
            velocity = (last_views - first_views) / time_diff
            return max(0.0, velocity)
            
        except Exception:
            return 0.0
    
    def _get_engagement_pattern(self, video_id: str, video_data: pd.DataFrame) -> List[float]:
        """Get engagement pattern for a video."""
        try:
            video_specific_data = video_data[video_data['video_id'] == video_id]
            
            if video_specific_data.empty:
                return []
            
            # Calculate engagement rate over time
            engagement_rates = []
            for _, row in video_specific_data.iterrows():
                engagement_rate = row['likes'] / max(1, row['views'])
                engagement_rates.append(engagement_rate)
            
            return engagement_rates
            
        except Exception:
            return []
    
    def _get_velocity_pattern(self, video_id: str, video_data: pd.DataFrame) -> List[float]:
        """Get velocity pattern for a video."""
        try:
            video_specific_data = video_data[video_data['video_id'] == video_id]
            
            if len(video_specific_data) < 2:
                return []
            
            # Calculate velocity over time
            velocities = []
            sorted_data = video_specific_data.sort_values('timestamp')
            
            for i in range(1, len(sorted_data)):
                prev_views = sorted_data.iloc[i-1]['views']
                curr_views = sorted_data.iloc[i]['views']
                time_diff = (pd.to_datetime(sorted_data.iloc[i]['timestamp']) - 
                           pd.to_datetime(sorted_data.iloc[i-1]['timestamp'])).total_seconds() / 3600
                
                if time_diff > 0:
                    velocity = (curr_views - prev_views) / time_diff
                    velocities.append(velocity)
            
            return velocities
            
        except Exception:
            return []
    
    def _calculate_engagement_overlap(self, pattern1: List[float], pattern2: List[float]) -> float:
        """Calculate engagement overlap between two patterns."""
        try:
            if not pattern1 or not pattern2:
                return 0.0
            
            # Normalize patterns to same length
            min_length = min(len(pattern1), len(pattern2))
            pattern1_norm = pattern1[:min_length]
            pattern2_norm = pattern2[:min_length]
            
            # Calculate correlation-based overlap
            if len(pattern1_norm) < 2:
                return 0.0
            
            # Simple correlation approximation
            mean1 = sum(pattern1_norm) / len(pattern1_norm)
            mean2 = sum(pattern2_norm) / len(pattern2_norm)
            
            if mean1 == 0 or mean2 == 0:
                return 0.0
            
            # Calculate overlap as inverse of relative difference
            relative_diff = abs(mean1 - mean2) / max(mean1, mean2)
            overlap = 1.0 - relative_diff
            
            return max(0.0, min(1.0, overlap))
            
        except Exception:
            return 0.0
    
    def _calculate_velocity_suppression(self, pattern1: List[float], pattern2: List[float]) -> float:
        """Calculate velocity suppression between two patterns."""
        try:
            if not pattern1 or not pattern2:
                return 0.0
            
            # Calculate average velocities
            avg_velocity1 = sum(pattern1) / len(pattern1)
            avg_velocity2 = sum(pattern2) / len(pattern2)
            
            # Calculate suppression as negative impact
            if avg_velocity1 <= 0 or avg_velocity2 <= 0:
                return 0.0
            
            # Suppression occurs when both videos have low velocity
            if avg_velocity1 < 100 and avg_velocity2 < 100:
                suppression = 1.0 - (avg_velocity1 + avg_velocity2) / 200
                return max(0.0, suppression)
            
            return 0.0
            
        except Exception:
            return 0.0
    
    def _update_tracking_data(self, video_id: str, signals: List[CannibalizationSignal]):
        """Update tracking data for learning."""
        try:
            # Update engagement patterns
            engagement_pattern = self._get_engagement_pattern(video_id, self.video_stats)
            if engagement_pattern:
                self.engagement_patterns[video_id] = engagement_pattern
            
            # Update velocity impacts
            velocity_impacts = [s.velocity_impact for s in signals if s.velocity_impact > 0]
            if velocity_impacts:
                self.velocity_impacts[video_id] = velocity_impacts
            
        except Exception as e:
            self.logger.error(f"Error updating tracking data: {e}")
    
    def generate_cannibalization_alert(self, 
                                      signals: List[CannibalizationSignal], 
                                      niche: str) -> Optional[CannibalizationAlert]:
        """Generate alert for cannibalization detection."""
        try:
            if not signals:
                return None
            
            # Determine overall severity
            severities = [s.severity for s in signals]
            if 'critical' in severities:
                overall_severity = 'critical'
            elif 'high' in severities:
                overall_severity = 'high'
            elif 'medium' in severities:
                overall_severity = 'medium'
            else:
                overall_severity = 'low'
            
            # Calculate maximum pause duration
            max_pause = max(s.pause_duration_hours for s in signals)
            
            # Get all affected videos
            affected_videos = list(set([s.video_id for s in signals] + 
                                     [comp for s in signals for comp in s.competing_videos]))
            
            # Generate alert message
            signal_types = list(set([s.cannibalization_type for s in signals]))
            signal_count = len(signals)
            
            message = f"Cannibalization detected: {signal_count} signals of types {signal_types}"
            
            # Generate recommendation
            if overall_severity == 'critical':
                recommendation = f"CRITICAL: Pause posting in niche {niche} for {max_pause} hours immediately"
                risk_assessment = "High risk of significant performance degradation"
            elif overall_severity == 'high':
                recommendation = f"URGENT: Pause posting in niche {niche} for {max_pause} hours"
                risk_assessment = "Moderate to high risk of performance impact"
            elif overall_severity == 'medium':
                recommendation = f"Consider pausing posting in niche {niche} for {max_pause} hours"
                risk_assessment = "Moderate risk of slight performance impact"
            else:
                recommendation = f"Monitor posting schedule in niche {niche}"
                risk_assessment = "Low risk, but monitoring recommended"
            
            alert = CannibalizationAlert(
                alert_id=f"cannibalization_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
                niche=niche,
                alert_type='cannibalization_detected',
                severity=overall_severity,
                message=message,
                detected_at=datetime.now(),
                signals=signals,
                recommendation=recommendation,
                pause_duration_hours=max_pause,
                affected_videos=affected_videos,
                risk_assessment=risk_assessment,
                next_review_date=datetime.now() + timedelta(hours=max_pause),
                auto_triggered=True
            )
            
            # Store alert
            self.alert_history.append(alert)
            
            self.logger.warning(f"Cannibalization alert generated: {recommendation}")
            
            return alert
            
        except Exception as e:
            self.logger.error(f"Error generating cannibalization alert: {e}")
            return None
    
    def get_cannibalization_dashboard(self) -> Dict:
        """Get comprehensive cannibalization detection dashboard."""
        try:
            # Recent signals (last 7 days)
            recent_cutoff = datetime.now() - timedelta(days=7)
            recent_signals = [s for s in self.detection_history if s.detected_at >= recent_cutoff]
            
            # Recent alerts (last 7 days)
            recent_alerts = [a for a in self.alert_history if a.detected_at >= recent_cutoff]
            
            # Calculate statistics
            total_signals = len(self.detection_history)
            total_alerts = len(self.alert_history)
            
            # Signal type distribution
            signal_types = {}
            for signal in recent_signals:
                signal_types[signal.cannibalization_type] = signal_types.get(signal.cannibalization_type, 0) + 1
            
            # Severity distribution
            severity_dist = {}
            for signal in recent_signals:
                severity_dist[signal.severity] = severity_dist.get(signal.severity, 0) + 1
            
            # Average risk score
            avg_risk_score = sum(s.risk_score for s in recent_signals) / len(recent_signals) if recent_signals else 0.0
            
            # Most affected niches
            niche_impacts = {}
            for alert in recent_alerts:
                niche_impacts[alert.niche] = niche_impacts.get(alert.niche, 0) + 1
            
            dashboard = {
                'detection_status': {
                    'total_signals_detected': total_signals,
                    'total_alerts_generated': total_alerts,
                    'recent_signals': len(recent_signals),
                    'recent_alerts': len(recent_alerts),
                    'average_risk_score': avg_risk_score
                },
                'signal_analysis': {
                    'signal_type_distribution': signal_types,
                    'severity_distribution': severity_dist,
                    'high_risk_signals': len([s for s in recent_signals if s.risk_score >= self.thresholds.high_risk_threshold]),
                    'critical_risk_signals': len([s for s in recent_signals if s.risk_score >= self.thresholds.critical_risk_threshold])
                },
                'niche_impacts': niche_impacts,
                'recent_alerts': [
                    {
                        'alert_id': alert.alert_id,
                        'severity': alert.severity,
                        'message': alert.message,
                        'recommendation': alert.recommendation,
                        'pause_duration_hours': alert.pause_duration_hours,
                        'affected_videos_count': len(alert.affected_videos)
                    }
                    for alert in recent_alerts[-5:]  # Last 5 alerts
                ],
                'prevention_stats': {
                    'total_pause_hours_recommended': sum(a.pause_duration_hours for a in recent_alerts),
                    'videos_protected': len(set([v for a in recent_alerts for v in a.affected_videos])),
                    'potential_budget_saved': len(recent_alerts) * 1000  # Estimated savings
                },
                'generated_at': datetime.now().isoformat()
            }
            
            return dashboard
            
        except Exception as e:
            self.logger.error(f"Error generating cannibalization dashboard: {e}")
            return {'error': str(e)}


# 🧯 7. Viral Anomaly Guardrails (Spike ≠ Success) - Fail Open, Never Fail Closed

@dataclass
class ViralAnomalySignals:
    """Multi-signal viral anomaly detection results."""
    engagement_ratio: float  # (likes+comments)/views ratio
    engagement_passed: bool  # Above 30% of niche median
    retention_rate: float  # 3-5s hold rate
    retention_passed: bool  # Above 25%
    velocity_continuity: float  # Spike decay half-life
    velocity_passed: bool  # Above 30 min
    geo_entropy: float  # Country diversity score
    geo_passed: bool  # Above threshold
    interaction_latency: float  # Like delay in seconds
    latency_passed: bool  # Within normal range
    comment_entropy: float  # Repeated phrases detection
    comment_passed: bool  # Below threshold
    signals_failed: List[str]
    signals_passed: List[str]


@dataclass
class ViralAnomalyScore:
    """Probabilistic anomaly scoring with confidence."""
    anomaly_probability: float  # 0.0 to 1.0
    confidence: float  # Confidence in the assessment
    signals_failed: List[str]
    signals_passed: List[str]
    recommendation: str  # 'treat_as_real', 'monitor', 'delay_spend', 'flag_for_review'
    quarantine_status: bool  # Under observation
    last_evaluation: datetime
    success_override: bool  # Success signal detected
    shadow_booster_enabled: bool  # Shadow validation active


@dataclass
class AnomalyQuarantineRecord:
    """Record of anomaly quarantine for observation."""
    video_id: str
    quarantine_start: datetime
    anomaly_probability: float
    signals_failed: List[str]
    quarantine_reason: str
    reevaluation_interval: timedelta
    max_quarantine_duration: timedelta
    organic_growth_allowed: bool
    expensive_boosters_blocked: bool
    status: str  # 'quarantined', 'cleared', 'extended'


class ViralAnomalyDetector:
    """
    Viral Anomaly Guardrails - Fail Open, Never Fail Closed.
    
    Core Principles:
    1. NEVER block real virality
    2. Fail open, never fail closed
    3. Observe, advise, degrade gracefully
    4. Multi-signal confirmation, not single rules
    5. Probabilistic scoring, not binary flags
    """
    
    def __init__(self):
        self.logger = logging.getLogger('ViralAnomalyDetector')
        
        # Anomaly detection history
        self.anomaly_history: Dict[str, ViralAnomalyScore] = {}
        self.quarantine_records: Dict[str, AnomalyQuarantineRecord] = {}
        
        # Success confirmation signals
        self.success_signals: Dict[str, List[str]] = {}
        
        # Shadow booster validation
        self.shadow_booster_results: Dict[str, Dict] = {}
        
        # Niche baselines for comparison
        self.niche_baselines: Dict[str, Dict] = {}
        
        # System safety flags
        self.system_healthy = True
        self.last_system_check = datetime.now()
        
    def detect_viral_anomaly(self, 
                           video_data: pd.DataFrame, 
                           video_id: str, 
                           niche: str) -> ViralAnomalyScore:
        """
        Detect viral anomalies using multi-signal confirmation.
        
        NEVER blocks growth - only provides confidence assessment.
        
        Args:
            video_data: DataFrame with video metrics
            video_id: Video identifier
            niche: Niche for baseline comparison
            
        Returns:
            ViralAnomalyScore: Probabilistic anomaly assessment
        """
        try:
            # 1️⃣ MULTI-SIGNAL VIRAL ANOMALY DETECTION
            signals = self._analyze_anomaly_signals(video_data, niche)
            
            # 2️⃣ PROBABILISTIC ANOMALY SCORING
            anomaly_probability = self._calculate_anomaly_probability(signals)
            
            # 3️⃣ SUCCESS OVERRIDE CHECK
            success_override = self._check_success_signals(video_id, video_data)
            
            # 4️⃣ TIME-BASED DECAY (if previously anomalous)
            if video_id in self.anomaly_history:
                anomaly_probability = self._apply_time_decay(video_id, anomaly_probability)
            
            # 5️⃣ DECISION RULES (never blocking)
            recommendation = self._get_recommendation(anomaly_probability, success_override)
            
            # 6️⃣ QUARANTINE DECISION (observation, not blocking)
            quarantine_status = self._should_quarantine(anomaly_probability, recommendation)
            
            # Create anomaly score
            anomaly_score = ViralAnomalyScore(
                anomaly_probability=anomaly_probability,
                confidence=self._calculate_confidence(signals),
                signals_failed=signals.signals_failed,
                signals_passed=signals.signals_passed,
                recommendation=recommendation,
                quarantine_status=quarantine_status,
                last_evaluation=datetime.now(),
                success_override=success_override,
                shadow_booster_enabled=anomaly_probability > 0.6
            )
            
            # Store in history
            self.anomaly_history[video_id] = anomaly_score
            
            # Create quarantine record if needed
            if quarantine_status:
                self._create_quarantine_record(video_id, anomaly_score, signals)
            
            # Log decision (full auditability)
            self._log_anomaly_decision(video_id, anomaly_score, signals)
            
            self.logger.info(f"Anomaly detection for {video_id}: probability={anomaly_probability:.2f}, "
                           f"recommendation={recommendation}, quarantine={quarantine_status}")
            
            return anomaly_score
            
        except Exception as e:
            self.logger.error(f"Error detecting viral anomaly for {video_id}: {e}")
            # 🛡️ FAIL OPEN: Return optimistic assessment on system failure
            return ViralAnomalyScore(
                anomaly_probability=0.1,  # Low suspicion
                confidence=0.3,  # Low confidence due to error
                signals_failed=[],
                signals_passed=[],
                recommendation='treat_as_real',
                quarantine_status=False,
                last_evaluation=datetime.now(),
                success_override=False,
                shadow_booster_enabled=False
            )
    
    def _analyze_anomaly_signals(self, video_data: pd.DataFrame, niche: str) -> ViralAnomalySignals:
        """Analyze multiple orthogonal signals for anomaly detection."""
        try:
            # Get niche baselines
            niche_baseline = self._get_niche_baseline(niche)
            
            # 1. Engagement Ratio Check
            engagement_ratio = self._calculate_engagement_ratio(video_data)
            engagement_passed = engagement_ratio >= (niche_baseline['engagement_ratio'] * 0.3)
            
            # 2. Retention Rate Check
            retention_rate = self._calculate_retention_rate(video_data)
            retention_passed = retention_rate >= 0.25  # 25% threshold
            
            # 3. Velocity Continuity Check
            velocity_continuity = self._calculate_velocity_continuity(video_data)
            velocity_passed = velocity_continuity >= 30  # 30 minutes threshold
            
            # 4. Geo Entropy Check
            geo_entropy = self._calculate_geo_entropy(video_data)
            geo_passed = geo_entropy >= niche_baseline['geo_entropy']
            
            # 5. Interaction Latency Check
            interaction_latency = self._calculate_interaction_latency(video_data)
            latency_passed = self._is_normal_latency(interaction_latency)
            
            # 6. Comment Entropy Check
            comment_entropy = self._calculate_comment_entropy(video_data)
            comment_passed = comment_entropy <= niche_baseline['comment_entropy']
            
            # Compile results
            signals_failed = []
            signals_passed = []
            
            if not engagement_passed:
                signals_failed.append('engagement')
            else:
                signals_passed.append('engagement')
                
            if not retention_passed:
                signals_failed.append('retention')
            else:
                signals_passed.append('retention')
                
            if not velocity_passed:
                signals_failed.append('velocity')
            else:
                signals_passed.append('velocity')
                
            if not geo_passed:
                signals_failed.append('geo')
            else:
                signals_passed.append('geo')
                
            if not latency_passed:
                signals_failed.append('latency')
            else:
                signals_passed.append('latency')
                
            if not comment_passed:
                signals_failed.append('comment')
            else:
                signals_passed.append('comment')
            
            return ViralAnomalySignals(
                engagement_ratio=engagement_ratio,
                engagement_passed=engagement_passed,
                retention_rate=retention_rate,
                retention_passed=retention_passed,
                velocity_continuity=velocity_continuity,
                velocity_passed=velocity_passed,
                geo_entropy=geo_entropy,
                geo_passed=geo_passed,
                interaction_latency=interaction_latency,
                latency_passed=latency_passed,
                comment_entropy=comment_entropy,
                comment_passed=comment_passed,
                signals_failed=signals_failed,
                signals_passed=signals_passed
            )
            
        except Exception as e:
            self.logger.error(f"Error analyzing anomaly signals: {e}")
            # 🛡️ FAIL OPEN: Assume signals passed on error
            return ViralAnomalySignals(
                engagement_ratio=0.05,
                engagement_passed=True,
                retention_rate=0.5,
                retention_passed=True,
                velocity_continuity=60,
                velocity_passed=True,
                geo_entropy=0.8,
                geo_passed=True,
                interaction_latency=30,
                latency_passed=True,
                comment_entropy=0.2,
                comment_passed=True,
                signals_failed=[],
                signals_passed=['engagement', 'retention', 'velocity', 'geo', 'latency', 'comment']
            )
    
    def _calculate_anomaly_probability(self, signals: ViralAnomalySignals) -> float:
        """
        Calculate probabilistic anomaly score.
        
        🔥 At least 3 signals must fail before anomaly classification.
        """
        try:
            failed_count = len(signals.signals_failed)
            total_signals = 6  # Total signals we check
            
            # Rule: Need at least 3 failed signals for significant anomaly
            if failed_count < 3:
                return 0.1 + (failed_count * 0.05)  # Low probability for < 3 failures
            
            # Calculate base probability from failed signals
            base_probability = failed_count / total_signals
            
            # Weight by signal importance
            critical_failures = ['retention', 'velocity', 'engagement']
            critical_failed = len([s for s in signals.signals_failed if s in critical_failures])
            
            if critical_failed >= 2:
                base_probability *= 1.5  # Boost probability for critical failures
            
            # Cap at 0.95 (never 100% certain)
            return min(0.95, base_probability)
            
        except Exception:
            return 0.1  # 🛡️ FAIL OPEN: Low suspicion on error
    
    def _check_success_signals(self, video_id: str, video_data: pd.DataFrame) -> bool:
        """
        Check for positive success confirmation signals.
        
        🟢 Any ONE success signal immediately overrides anomaly suspicion.
        """
        try:
            # 1. Retention curve stabilization
            if self._has_stabilizing_retention(video_data):
                return True
            
            # 2. Engagement rising post-spike
            if self._has_rising_engagement(video_data):
                return True
            
            # 3. Velocity re-acceleration
            if self._has_velocity_recovery(video_data):
                return True
            
            # 4. Follow-on video lift
            if self._has_followon_lift(video_id):
                return True
            
            # 5. Cross-platform spillover
            if self._has_cross_platform_spillover(video_id):
                return True
            
            return False
            
        except Exception:
            return False  # 🛡️ FAIL OPEN: No success signal on error
    
    def _apply_time_decay(self, video_id: str, current_probability: float) -> float:
        """Apply time-based decay to anomaly scores."""
        try:
            if video_id not in self.anomaly_history:
                return current_probability
            
            previous_score = self.anomaly_history[video_id]
            time_since_last = datetime.now() - previous_score.last_evaluation
            
            # Decay rates based on time windows
            if time_since_last <= timedelta(minutes=10):
                return current_probability  # No decay yet
            elif time_since_last <= timedelta(minutes=30):
                decay_factor = 0.9
            elif time_since_last <= timedelta(hours=2):
                decay_factor = 0.7
            elif time_since_last <= timedelta(hours=24):
                decay_factor = 0.5
            else:
                decay_factor = 0.3  # Significant decay after 24 hours
            
            return current_probability * decay_factor
            
        except Exception:
            return current_probability  # No decay on error
    
    def _get_recommendation(self, anomaly_probability: float, success_override: bool) -> str:
        """
        Get recommendation based on anomaly probability.
        
        🎯 DECISION RULES - NEVER BLOCKING
        """
        try:
            # Success signal has veto power
            if success_override:
                return 'treat_as_real'
            
            if anomaly_probability < 0.3:
                return 'treat_as_real'  # Treat as real virality
            elif anomaly_probability <= 0.6:
                return 'monitor'  # Monitor, no boosters
            elif anomaly_probability <= 0.8:
                return 'delay_spend'  # Delay aggressive spend
            else:
                return 'flag_for_review'  # Flag for review (still no blocking)
                
        except Exception:
            return 'treat_as_real'  # 🛡️ FAIL OPEN: Assume real virality
    
    def _should_quarantine(self, anomaly_probability: float, recommendation: str) -> bool:
        """Determine if video should be quarantined for observation."""
        try:
            # Only quarantine for monitoring or delay_spend recommendations
            if recommendation in ['monitor', 'delay_spend'] and anomaly_probability > 0.4:
                return True
            return False
            
        except Exception:
            return False  # 🛡️ FAIL OPEN: No quarantine on error
    
    def _create_quarantine_record(self, video_id: str, anomaly_score: ViralAnomalyScore, signals: ViralAnomalySignals):
        """Create quarantine record for safe observation."""
        try:
            quarantine = AnomalyQuarantineRecord(
                video_id=video_id,
                quarantine_start=datetime.now(),
                anomaly_probability=anomaly_score.anomaly_probability,
                signals_failed=signals.signals_failed,
                quarantine_reason=f"Anomaly probability {anomaly_score.anomaly_probability:.2f}",
                reevaluation_interval=timedelta(minutes=30),  # Re-evaluate every 30 min
                max_quarantine_duration=timedelta(hours=24),  # Max 24 hours quarantine
                organic_growth_allowed=True,  # NEVER block organic growth
                expensive_boosters_blocked=True,  # Only block expensive actions
                status='quarantined'
            )
            
            self.quarantine_records[video_id] = quarantine
            
        except Exception as e:
            self.logger.error(f"Error creating quarantine record for {video_id}: {e}")
    
    def _log_anomaly_decision(self, video_id: str, anomaly_score: ViralAnomalyScore, signals: ViralAnomalySignals):
        """Full auditability for every anomaly decision."""
        try:
            decision_log = {
                'video_id': video_id,
                'timestamp': datetime.now().isoformat(),
                'anomaly_probability': anomaly_score.anomaly_probability,
                'confidence': anomaly_score.confidence,
                'signals_failed': signals.signals_failed,
                'signals_passed': signals.signals_passed,
                'recommendation': anomaly_score.recommendation,
                'quarantine_status': anomaly_score.quarantine_status,
                'success_override': anomaly_score.success_override,
                'system_healthy': self.system_healthy
            }
            
            self.logger.info(f"ANOMALY_DECISION: {decision_log}")
            
        except Exception as e:
            self.logger.error(f"Error logging anomaly decision: {e}")
    
    def reevaluate_quarantined_video(self, video_id: str, video_data: pd.DataFrame, niche: str) -> ViralAnomalyScore:
        """Re-evaluate a quarantined video."""
        try:
            if video_id not in self.quarantine_records:
                return self.detect_viral_anomaly(video_data, video_id, niche)
            
            quarantine = self.quarantine_records[video_id]
            
            # Check if quarantine should be extended or cleared
            time_in_quarantine = datetime.now() - quarantine.quarantine_start
            
            if time_in_quarantine >= quarantine.max_quarantine_duration:
                # Clear quarantine after max duration
                del self.quarantine_records[video_id]
                self.logger.info(f"Quarantine expired for {video_id}, clearing automatically")
            
            # Re-evaluate anomaly status
            new_score = self.detect_viral_anomaly(video_data, video_id, niche)
            
            # Update quarantine status
            if new_score.quarantine_status:
                quarantine.status = 'extended'
            else:
                quarantine.status = 'cleared'
                del self.quarantine_records[video_id]
                self.logger.info(f"Quarantine cleared for {video_id}")
            
            return new_score
            
        except Exception as e:
            self.logger.error(f"Error re-evaluating quarantine for {video_id}: {e}")
            # 🛡️ FAIL OPEN: Clear quarantine on error
            if video_id in self.quarantine_records:
                del self.quarantine_records[video_id]
            return self.detect_viral_anomaly(video_data, video_id, niche)
    
    def get_anomaly_dashboard(self) -> Dict:
        """Get comprehensive anomaly detection dashboard."""
        try:
            # Current statistics
            total_evaluations = len(self.anomaly_history)
            active_quarantines = len(self.quarantine_records)
            
            # Anomaly distribution
            high_anomaly_count = len([s for s in self.anomaly_history.values() if s.anomaly_probability > 0.6])
            medium_anomaly_count = len([s for s in self.anomaly_history.values() if 0.3 <= s.anomaly_probability <= 0.6])
            low_anomaly_count = len([s for s in self.anomaly_history.values() if s.anomaly_probability < 0.3])
            
            # Recent evaluations (last 24 hours)
            recent_cutoff = datetime.now() - timedelta(hours=24)
            recent_evaluations = [s for s in self.anomaly_history.values() if s.last_evaluation >= recent_cutoff]
            
            # Success overrides (prevented false negatives)
            success_overrides = len([s for s in self.anomaly_history.values() if s.success_override])
            
            dashboard = {
                'anomaly_detection_status': {
                    'total_evaluations': total_evaluations,
                    'active_quarantines': active_quarantines,
                    'recent_evaluations_24h': len(recent_evaluations),
                    'success_overrides': success_overrides,
                    'system_healthy': self.system_healthy,
                    'last_system_check': self.last_system_check.isoformat()
                },
                'anomaly_distribution': {
                    'high_anomaly_count': high_anomaly_count,
                    'medium_anomaly_count': medium_anomaly_count,
                    'low_anomaly_count': low_anomaly_count,
                    'average_anomaly_probability': sum(s.anomaly_probability for s in self.anomaly_history.values()) / max(1, total_evaluations)
                },
                'recommendation_distribution': {
                    'treat_as_real': len([s for s in self.anomaly_history.values() if s.recommendation == 'treat_as_real']),
                    'monitor': len([s for s in self.anomaly_history.values() if s.recommendation == 'monitor']),
                    'delay_spend': len([s for s in self.anomaly_history.values() if s.recommendation == 'delay_spend']),
                    'flag_for_review': len([s for s in self.anomaly_history.values() if s.recommendation == 'flag_for_review'])
                },
                'quarantine_status': {
                    'total_quarantined': active_quarantines,
                    'quarantine_records': [
                        {
                            'video_id': record.video_id,
                            'quarantine_start': record.quarantine_start.isoformat(),
                            'anomaly_probability': record.anomaly_probability,
                            'signals_failed': record.signals_failed,
                            'status': record.status,
                            'organic_growth_allowed': record.organic_growth_allowed
                        }
                        for record in self.quarantine_records.values()
                    ]
                },
                'safety_metrics': {
                    'false_negative_prevention': success_overrides,
                    'time_decay_applied': len([s for s in self.anomaly_history.values() if s.anomaly_probability < 0.5]),
                    'shadow_boosters_active': len([s for s in self.anomaly_history.values() if s.shadow_booster_enabled]),
                    'fail_open_events': len([s for s in self.anomaly_history.values() if s.confidence < 0.5])
                },
                'generated_at': datetime.now().isoformat()
            }
            
            return dashboard
            
        except Exception as e:
            self.logger.error(f"Error generating anomaly dashboard: {e}")
            return {'error': str(e)}
    
    # Helper methods for signal analysis
    def _get_niche_baseline(self, niche: str) -> Dict:
        """Get niche baseline metrics for comparison."""
        default_baseline = {
            'engagement_ratio': 0.05,
            'geo_entropy': 0.6,
            'comment_entropy': 0.4
        }
        return self.niche_baselines.get(niche, default_baseline)
    
    def _calculate_engagement_ratio(self, video_data: pd.DataFrame) -> float:
        """Calculate (likes+comments)/views ratio."""
        try:
            if video_data.empty:
                return 0.05
            
            latest_data = video_data.iloc[-1]
            views = max(1, latest_data['views'])
            engagement = latest_data['likes'] + latest_data['comments']
            return engagement / views
            
        except Exception:
            return 0.05  # 🛡️ FAIL OPEN: Normal engagement on error
    
    def _calculate_retention_rate(self, video_data: pd.DataFrame) -> float:
        """Calculate 3-5s retention rate."""
        try:
            # Mock implementation - would use real retention data
            return 0.4  # 40% retention rate
            
        except Exception:
            return 0.5  # 🛡️ FAIL OPEN: Good retention on error
    
    def _calculate_velocity_continuity(self, video_data: pd.DataFrame) -> float:
        """Calculate spike decay half-life in minutes."""
        try:
            if len(video_data) < 3:
                return 60  # Default 60 minutes
            
            # Calculate velocity decay
            views = video_data['views'].values
            timestamps = pd.to_datetime(video_data['timestamp'])
            
            # Find peak velocity
            velocities = np.diff(views) / np.diff(timestamps).astype('timedelta64[s]').astype(float) / 3600
            peak_idx = np.argmax(velocities)
            
            # Calculate decay to half of peak
            peak_velocity = velocities[peak_idx]
            half_peak = peak_velocity / 2
            
            for i in range(peak_idx + 1, len(velocities)):
                if velocities[i] <= half_peak:
                    decay_time = (timestamps[i] - timestamps[peak_idx]).total_seconds() / 60
                    return decay_time
            
            return 120  # 2 hours if no decay found
            
        except Exception:
            return 60  # 🛡️ FAIL OPEN: Normal continuity on error
    
    def _calculate_geo_entropy(self, video_data: pd.DataFrame) -> float:
        """Calculate geographic diversity entropy."""
        try:
            # Mock implementation - would use real geo data
            return 0.7  # Good geo diversity
            
        except Exception:
            return 0.8  # 🛡️ FAIL OPEN: High diversity on error
    
    def _calculate_interaction_latency(self, video_data: pd.DataFrame) -> float:
        """Calculate average like/comment delay."""
        try:
            # Mock implementation - would use real interaction timing
            return 30  # 30 seconds average
            
        except Exception:
            return 45  # 🛡️ FAIL OPEN: Normal latency on error
    
    def _is_normal_latency(self, latency: float) -> bool:
        """Check if interaction latency is within normal range."""
        return 10 <= latency <= 300  # 10 seconds to 5 minutes
    
    def _calculate_comment_entropy(self, video_data: pd.DataFrame) -> float:
        """Calculate comment entropy (repeated phrases detection)."""
        try:
            # Mock implementation - would use real comment analysis
            return 0.3  # Low repetition
            
        except Exception:
            return 0.2  # 🛡️ FAIL OPEN: Low repetition on error
    
    # Success signal detection methods
    def _has_stabilizing_retention(self, video_data: pd.DataFrame) -> bool:
        """Check if retention curve is stabilizing."""
        try:
            # Mock implementation - would analyze retention curve
            return False
            
        except Exception:
            return False
    
    def _has_rising_engagement(self, video_data: pd.DataFrame) -> bool:
        """Check if engagement is rising post-spike."""
        try:
            if len(video_data) < 5:
                return False
            
            engagement_rates = (video_data['likes'] + video_data['comments']) / video_data['views']
            recent_trend = np.polyfit(range(5), engagement_rates.tail(5), 1)[0]
            return recent_trend > 0.001  # Positive trend
            
        except Exception:
            return False
    
    def _calculate_confidence(self, signals: ViralAnomalySignals) -> float:
        """Calculate confidence in anomaly assessment."""
        try:
            total_signals = 6
            failed_signals = len(signals.signals_failed)
            
            # Higher confidence with more signal data
            base_confidence = 0.5 + (total_signals - failed_signals) * 0.08
            
            # Reduce confidence if system has issues
            if not self.system_healthy:
                base_confidence *= 0.7
            
            return min(0.95, max(0.3, base_confidence))
            
        except Exception:
            return 0.5
    
    def _calculate_compounding_factor(self, outcome_score: float, confidence_level: float) -> float:
        """Calculate how much this action influences future decisions."""
        try:
            # Higher outcomes and confidence = more influence
            base_factor = abs(outcome_score) * confidence_level
            
            # Positive outcomes have more influence than negative
            if outcome_score > 0:
                base_factor *= 1.2
            
            return min(1.0, base_factor)
            
        except Exception:
            return 0.1


# 🧠 9. Factory Learning Memory - Compounding Advantage System

class FactoryLearningMemory:
    """
    Factory Learning Memory - Compounding Advantage System.
    
    Persists what works, where, and when to continuously improve factory performance.
    """
    
    def __init__(self):
        self.logger = logging.getLogger('FactoryLearningMemory')
        
        # Learning storage
        self.action_memories: List[FactoryActionMemory] = []
        self.niche_profiles: Dict[str, NicheLearningProfile] = {}
        self.lifecycle_insights: Dict[str, LifecycleStageInsights] = {}
        
        # Learning configuration
        self.min_actions_for_insights = 5
        self.compounding_decay_rate = 0.95  # Older insights decay over time
        self.learning_maturity_threshold = 0.7
        
        # Initialize lifecycle stages
        self._initialize_lifecycle_stages()
        
    def _initialize_lifecycle_stages(self):
        """Initialize lifecycle stage tracking."""
        stages = ['ignition', 'growth', 'maturity', 'decline']
        for stage in stages:
            self.lifecycle_insights[stage] = LifecycleStageInsights(
                stage=stage,
                total_observations=0,
                success_rates={},
                optimal_parameters={},
                timing_patterns={},
                risk_factors=[],
                success_factors=[],
                confidence_scores={}
            )
    
    def record_action_outcome(self, 
                            action_type: str,
                            niche: str, 
                            video_id: str,
                            lifecycle_stage: str,
                            pre_metrics: Dict[str, float],
                            post_metrics: Dict[str, float],
                            action_parameters: Dict[str, Any],
                            duration_days: int = 7) -> str:
        """
        Record the outcome of a factory action for learning.
        
        Args:
            action_type: Type of action taken
            niche: Factory niche
            video_id: Video identifier
            lifecycle_stage: Current lifecycle stage
            pre_metrics: Metrics before action
            post_metrics: Metrics after action
            action_parameters: Parameters used in action
            duration_days: How long to track the outcome
            
        Returns:
            str: Action memory ID
        """
        try:
            # Calculate outcome score
            outcome_score = self._calculate_outcome_score(pre_metrics, post_metrics)
            
            # Determine effectiveness rating
            effectiveness_rating = self._determine_effectiveness(outcome_score)
            
            # Calculate confidence level
            confidence_level = self._calculate_confidence_level(pre_metrics, post_metrics, action_parameters)
            
            # Calculate compounding factor
            compounding_factor = self._calculate_compounding_factor(outcome_score, confidence_level)
            
            # Create action memory
            action_memory = FactoryActionMemory(
                action_id=f"{action_type}_{video_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
                action_type=action_type,
                niche=niche,
                video_id=video_id,
                lifecycle_stage=lifecycle_stage,
                pre_action_metrics=pre_metrics,
                post_action_metrics=post_metrics,
                action_parameters=action_parameters,
                outcome_score=outcome_score,
                effectiveness_rating=effectiveness_rating,
                confidence_level=confidence_level,
                timestamp=datetime.now(),
                duration_days=duration_days,
                compounding_factor=compounding_factor
            )
            
            # Store memory
            self.action_memories.append(action_memory)
            
            # Update learning profiles
            self._update_niche_profile(action_memory)
            self._update_lifecycle_insights(action_memory)
            
            self.logger.info(f"Recorded action outcome: {action_type} on {video_id} - "
                           f"score: {outcome_score:.2f}, rating: {effectiveness_rating}")
            
            return action_memory.action_id
            
        except Exception as e:
            self.logger.error(f"Error recording action outcome: {e}")
            return ""
    
    def _calculate_outcome_score(self, pre_metrics: Dict[str, float], post_metrics: Dict[str, float]) -> float:
        """Calculate outcome score based on metric improvements."""
        try:
            # Key metrics to track
            key_metrics = ['views', 'engagement_rate', 'growth_velocity', 'virality_score']
            
            total_improvement = 0.0
            metric_count = 0
            
            for metric in key_metrics:
                if metric in pre_metrics and metric in post_metrics:
                    pre_val = pre_metrics[metric]
                    post_val = post_metrics[metric]
                    
                    if pre_val > 0:
                        improvement = (post_val - pre_val) / pre_val
                        total_improvement += improvement
                        metric_count += 1
            
            if metric_count == 0:
                return 0.0
            
            # Normalize to -1 to 1 range
            avg_improvement = total_improvement / metric_count
            return max(-1.0, min(1.0, avg_improvement))
            
        except Exception:
            return 0.0
    
    def _determine_effectiveness(self, outcome_score: float) -> str:
        """Determine effectiveness rating from outcome score."""
        if outcome_score >= 0.5:
            return 'highly_effective'
        elif outcome_score >= 0.2:
            return 'effective'
        elif outcome_score >= -0.1:
            return 'neutral'
        else:
            return 'ineffective'
    
    def _calculate_confidence_level(self, pre_metrics: Dict[str, float], post_metrics: Dict[str, float], 
                                  action_parameters: Dict[str, Any]) -> float:
        """Calculate confidence in the assessment."""
        try:
            # Base confidence from data quality
            base_confidence = 0.5
            
            # More metrics = higher confidence
            metric_count = len([k for k in ['views', 'engagement_rate', 'growth_velocity'] 
                              if k in pre_metrics and k in post_metrics])
            base_confidence += (metric_count / 3) * 0.3
            
            # Parameter completeness
            param_completeness = len(action_parameters) / 5  # Assume 5 key parameters
            base_confidence += param_completeness * 0.2
            
            return min(0.95, max(0.3, base_confidence))
            
        except Exception:
            return 0.5
    
    def _calculate_compounding_factor(self, outcome_score: float, confidence_level: float) -> float:
        """Calculate how much this action influences future decisions."""
        try:
            # Higher outcomes and confidence = more influence
            base_factor = abs(outcome_score) * confidence_level
            
            # Positive outcomes have more influence than negative
            if outcome_score > 0:
                base_factor *= 1.2
            
            return min(1.0, base_factor)
            
        except Exception:
            return 0.1
    
    def _update_niche_profile(self, action_memory: FactoryActionMemory):
        """Update learning profile for the niche."""
        try:
            niche = action_memory.niche
            
            if niche not in self.niche_profiles:
                self.niche_profiles[niche] = NicheLearningProfile(
                    niche=niche,
                    total_actions=0,
                    successful_actions=0,
                    failed_actions=0,
                    most_effective_actions={},
                    lifecycle_preferences={},
                    optimal_timing_windows={},
                    parameter_insights={},
                    last_updated=datetime.now(),
                    learning_maturity=0.0
                )
            
            profile = self.niche_profiles[niche]
            
            # Update counts
            profile.total_actions += 1
            if action_memory.effectiveness_rating in ['highly_effective', 'effective']:
                profile.successful_actions += 1
            else:
                profile.failed_actions += 1
            
            # Update most effective actions
            action_type = action_memory.action_type
            if action_type not in profile.most_effective_actions:
                profile.most_effective_actions[action_type] = []
            
            # Add outcome to history
            profile.most_effective_actions[action_type].append(action_memory.outcome_score)
            
            # Update lifecycle preferences
            if action_memory.lifecycle_stage not in profile.lifecycle_preferences:
                profile.lifecycle_preferences[action_memory.lifecycle_stage] = {}
            
            if action_type not in profile.lifecycle_preferences[action_memory.lifecycle_stage]:
                profile.lifecycle_preferences[action_memory.lifecycle_stage][action_type] = []
            
            profile.lifecycle_preferences[action_memory.lifecycle_stage][action_type].append(action_memory.outcome_score)
            
            # Update optimal timing
            hour = action_memory.timestamp.hour
            if action_type not in profile.optimal_timing_windows:
                profile.optimal_timing_windows[action_type] = []
            
            profile.optimal_timing_windows[action_type].append((hour, action_memory.outcome_score))
            
            # Update parameter insights
            if action_type not in profile.parameter_insights:
                profile.parameter_insights[action_type] = {}
            
            for param, value in action_memory.action_parameters.items():
                if param not in profile.parameter_insights[action_type]:
                    profile.parameter_insights[action_type][param] = []
                
                profile.parameter_insights[action_type][param].append((value, action_memory.outcome_score))
            
            # Update learning maturity
            if profile.total_actions >= self.min_actions_for_insights:
                success_rate = profile.successful_actions / profile.total_actions
                profile.learning_maturity = min(1.0, success_rate * 2)
            
            profile.last_updated = datetime.now()
            
        except Exception as e:
            self.logger.error(f"Error updating niche profile: {e}")
    
    def _update_lifecycle_insights(self, action_memory: FactoryActionMemory):
        """Update lifecycle stage insights."""
        try:
            stage = action_memory.lifecycle_stage
            insights = self.lifecycle_insights[stage]
            
            # Update observations
            insights.total_observations += 1
            
            # Update success rates
            action_type = action_memory.action_type
            if action_type not in insights.success_rates:
                insights.success_rates[action_type] = []
            
            insights.success_rates[action_type].append(action_memory.outcome_score)
            
            # Update optimal parameters
            if action_type not in insights.optimal_parameters:
                insights.optimal_parameters[action_type] = {}
            
            for param, value in action_memory.action_parameters.items():
                if param not in insights.optimal_parameters[action_type]:
                    insights.optimal_parameters[action_type][param] = []
                
                insights.optimal_parameters[action_type][param].append((value, action_memory.outcome_score))
            
            # Update timing patterns
            hour = action_memory.timestamp.hour
            if action_type not in insights.timing_patterns:
                insights.timing_patterns[action_type] = []
            
            insights.timing_patterns[action_type].append((hour, action_memory.outcome_score))
            
            # Update confidence scores
            if action_type not in insights.confidence_scores:
                insights.confidence_scores[action_type] = []
            
            insights.confidence_scores[action_type].append(action_memory.confidence_level)
            
        except Exception as e:
            self.logger.error(f"Error updating lifecycle insights: {e}")
    
    def get_action_recommendations(self, niche: str, lifecycle_stage: str, current_metrics: Dict[str, float]) -> Dict:
        """
        Get action recommendations based on learning memory.
        
        Args:
            niche: Factory niche
            lifecycle_stage: Current lifecycle stage
            current_metrics: Current video metrics
            
        Returns:
            Dict: Action recommendations with confidence scores
        """
        try:
            recommendations = {
                'niche': niche,
                'lifecycle_stage': lifecycle_stage,
                'recommended_actions': [],
                'confidence_level': 0.0,
                'learning_maturity': 0.0,
                'parameter_suggestions': {},
                'timing_suggestions': {},
                'risk_assessment': {},
                'compounding_advantage': 0.0
            }
            
            # Get niche profile
            niche_profile = self.niche_profiles.get(niche)
            if not niche_profile or niche_profile.total_actions < self.min_actions_for_insights:
                return recommendations
            
            # Get lifecycle insights
            stage_insights = self.lifecycle_insights.get(lifecycle_stage)
            if not stage_insights or stage_insights.total_observations < self.min_actions_for_insights:
                return recommendations
            
            # Calculate base confidence
            recommendations['learning_maturity'] = niche_profile.learning_maturity
            recommendations['confidence_level'] = niche_profile.learning_maturity * 0.8
            
            # Get action preferences for this stage
            stage_preferences = niche_profile.lifecycle_preferences.get(lifecycle_stage, {})
            
            # Rank actions by effectiveness
            action_rankings = []
            for action_type, outcomes in stage_preferences.items():
                if len(outcomes) >= 3:  # Minimum data points
                    avg_outcome = sum(outcomes) / len(outcomes)
                    action_rankings.append((action_type, avg_outcome, len(outcomes)))
            
            # Sort by effectiveness
            action_rankings.sort(key=lambda x: x[1], reverse=True)
            
            # Generate recommendations
            for action_type, avg_outcome, data_points in action_rankings[:5]:  # Top 5
                if avg_outcome > 0.1:  # Only recommend positive outcomes
                    # Get optimal parameters
                    optimal_params = self._get_optimal_parameters(niche, action_type, lifecycle_stage)
                    
                    # Get optimal timing
                    optimal_timing = self._get_optimal_timing(niche, action_type)
                    
                    # Calculate confidence for this specific recommendation
                    action_confidence = min(0.95, avg_outcome * niche_profile.learning_maturity)
                    
                    recommendation = {
                        'action_type': action_type,
                        'expected_outcome': avg_outcome,
                        'confidence': action_confidence,
                        'data_points': data_points,
                        'optimal_parameters': optimal_params,
                        'optimal_timing_hours': optimal_timing,
                        'risk_level': self._assess_action_risk(action_type, lifecycle_stage, avg_outcome)
                    }
                    
                    recommendations['recommended_actions'].append(recommendation)
            
            # Calculate compounding advantage
            recommendations['compounding_advantage'] = self._calculate_compounding_advantage(niche)
            
            return recommendations
            
        except Exception as e:
            self.logger.error(f"Error getting action recommendations: {e}")
            return {'error': str(e)}
    
    def _get_optimal_parameters(self, niche: str, action_type: str, lifecycle_stage: str) -> Dict[str, Any]:
        """Get optimal parameters for an action in a specific context."""
        try:
            niche_profile = self.niche_profiles.get(niche)
            if not niche_profile:
                return {}
            
            param_insights = niche_profile.parameter_insights.get(action_type, {})
            optimal_params = {}
            
            for param, value_outcome_pairs in param_insights.items():
                if len(value_outcome_pairs) >= 3:
                    # Find parameter value with best average outcome
                    param_values = {}
                    for value, outcome in value_outcome_pairs:
                        if value not in param_values:
                            param_values[value] = []
                        param_values[value].append(outcome)
                    
                    # Calculate average outcome for each parameter value
                    best_value = None
                    best_avg_outcome = -999
                    
                    for value, outcomes in param_values.items():
                        avg_outcome = sum(outcomes) / len(outcomes)
                        if avg_outcome > best_avg_outcome:
                            best_avg_outcome = avg_outcome
                            best_value = value
                    
                    if best_value is not None:
                        optimal_params[param] = best_value
            
            return optimal_params
            
        except Exception:
            return {}
    
    def _get_optimal_timing(self, niche: str, action_type: str) -> List[int]:
        """Get optimal timing hours for an action."""
        try:
            niche_profile = self.niche_profiles.get(niche)
            if not niche_profile:
                return []
            
            timing_data = niche_profile.optimal_timing_windows.get(action_type, [])
            if len(timing_data) < 5:
                return []
            
            # Group by hour and calculate average outcomes
            hour_outcomes = {}
            for hour, outcome in timing_data:
                if hour not in hour_outcomes:
                    hour_outcomes[hour] = []
                hour_outcomes[hour].append(outcome)
            
            # Calculate average outcome per hour
            hour_avg_outcomes = {}
            for hour, outcomes in hour_outcomes.items():
                hour_avg_outcomes[hour] = sum(outcomes) / len(outcomes)
            
            # Sort by outcome and return top hours
            sorted_hours = sorted(hour_avg_outcomes.items(), key=lambda x: x[1], reverse=True)
            return [hour for hour, _ in sorted_hours[:3]]  # Top 3 hours
            
        except Exception:
            return []
    
    def _assess_action_risk(self, action_type: str, lifecycle_stage: str, expected_outcome: float) -> str:
        """Assess risk level for an action."""
        try:
            # Base risk from lifecycle stage
            stage_risks = {
                'ignition': 'high',
                'growth': 'medium',
                'maturity': 'low',
                'decline': 'high'
            }
            
            base_risk = stage_risks.get(lifecycle_stage, 'medium')
            
            # Adjust based on expected outcome
            if expected_outcome > 0.5:
                return 'low'
            elif expected_outcome > 0.2:
                return base_risk
            else:
                return 'high'
                
        except Exception:
            return 'medium'
    
    def _calculate_compounding_advantage(self, niche: str) -> float:
        """Calculate the compounding learning advantage for a niche."""
        try:
            niche_profile = self.niche_profiles.get(niche)
            if not niche_profile:
                return 0.0
            
            # Base advantage from learning maturity
            base_advantage = niche_profile.learning_maturity
            
            # Boost from successful actions
            if niche_profile.total_actions > 0:
                success_rate = niche_profile.successful_actions / niche_profile.total_actions
                base_advantage *= (1 + success_rate)
            
            # Recent success bonus
            recent_actions = [m for m in self.action_memories 
                             if m.niche == niche and 
                             (datetime.now() - m.timestamp).days <= 30]
            
            if recent_actions:
                recent_success_rate = len([m for m in recent_actions 
                                         if m.effectiveness_rating in ['highly_effective', 'effective']]) / len(recent_actions)
                base_advantage *= (1 + recent_success_rate * 0.5)
            
            return min(2.0, base_advantage)  # Cap at 2x advantage
            
        except Exception:
            return 0.0
    
    def get_learning_dashboard(self) -> Dict:
        """Get comprehensive learning dashboard."""
        try:
            dashboard = {
                'learning_overview': {
                    'total_actions_recorded': len(self.action_memories),
                    'niches_with_learning': len(self.niche_profiles),
                    'average_learning_maturity': sum(p.learning_maturity for p in self.niche_profiles.values()) / max(1, len(self.niche_profiles)),
                    'total_compounding_advantage': sum(self._calculate_compounding_advantage(n) for n in self.niche_profiles.keys())
                },
                'niche_profiles': {},
                'lifecycle_insights': {},
                'top_performing_actions': {},
                'learning_trends': {},
                'parameter_optimizations': {},
                'generated_at': datetime.now().isoformat()
            }
            
            # Niche profiles
            for niche, profile in self.niche_profiles.items():
                dashboard['niche_profiles'][niche] = {
                    'total_actions': profile.total_actions,
                    'success_rate': profile.successful_actions / max(1, profile.total_actions),
                    'learning_maturity': profile.learning_maturity,
                    'compounding_advantage': self._calculate_compounding_advantage(niche),
                    'most_effective_actions': dict((k, sum(v) / len(v)) for k, v in profile.most_effective_actions.items() if v),
                    'last_updated': profile.last_updated.isoformat()
                }
            
            # Lifecycle insights
            for stage, insights in self.lifecycle_insights.items():
                dashboard['lifecycle_insights'][stage] = {
                    'total_observations': insights.total_observations,
                    'action_success_rates': dict((k, sum(v) / len(v)) for k, v in insights.success_rates.items() if v),
                    'optimal_timing_windows': dict((k, self._get_optimal_timing_for_stage(k, insights.timing_patterns.get(k, []))) for k in insights.timing_patterns.keys()),
                    'average_confidence': sum(insights.confidence_scores.values()) / max(1, len(insights.confidence_scores))
                }
            
            # Top performing actions across all niches
            action_performance = {}
            for memory in self.action_memories:
                action_type = memory.action_type
                if action_type not in action_performance:
                    action_performance[action_type] = []
                action_performance[action_type].append(memory.outcome_score)
            
            dashboard['top_performing_actions'] = dict(
                (k, sum(v) / len(v)) for k, v in action_performance.items() if v
            )
            
            # Sort by performance
            dashboard['top_performing_actions'] = dict(
                sorted(dashboard['top_performing_actions'].items(), key=lambda x: x[1], reverse=True)
            )
            
            return dashboard
            
        except Exception as e:
            self.logger.error(f"Error generating learning dashboard: {e}")
            return {'error': str(e)}
    
    def _get_optimal_timing_for_stage(self, action_type: str, timing_data: List[Tuple[int, float]]) -> List[int]:
        """Get optimal timing for a specific action type."""
        try:
            if len(timing_data) < 3:
                return []
            
            # Group by hour and calculate average outcomes
            hour_outcomes = {}
            for hour, outcome in timing_data:
                if hour not in hour_outcomes:
                    hour_outcomes[hour] = []
                hour_outcomes[hour].append(outcome)
            
            # Calculate average outcome per hour
            hour_avg_outcomes = {}
            for hour, outcomes in hour_outcomes.items():
                hour_avg_outcomes[hour] = sum(outcomes) / len(outcomes)
            
            # Sort by outcome and return top hours
            sorted_hours = sorted(hour_avg_outcomes.items(), key=lambda x: x[1], reverse=True)
            return [hour for hour, _ in sorted_hours[:3]]  # Top 3 hours
            
        except Exception:
            return []
    
    def export_learning_data(self, file_path: str) -> bool:
        """Export learning data for backup or analysis."""
        try:
            import json
            
            export_data = {
                'action_memories': [asdict(m) for m in self.action_memories],
                'niche_profiles': {k: asdict(v) for k, v in self.niche_profiles.items()},
                'lifecycle_insights': {k: asdict(v) for k, v in self.lifecycle_insights.items()},
                'export_timestamp': datetime.now().isoformat()
            }
            
            # Convert datetime objects to strings for JSON serialization
            def convert_datetime(obj):
                if isinstance(obj, datetime):
                    return obj.isoformat()
                elif isinstance(obj, dict):
                    return {k: convert_datetime(v) for k, v in obj.items()}
                elif isinstance(obj, list):
                    return [convert_datetime(item) for item in obj]
                else:
                    return obj
            
            export_data = convert_datetime(export_data)
            
            with open(file_path, 'w') as f:
                json.dump(export_data, f, indent=2)
            
            self.logger.info(f"Learning data exported to {file_path}")
            return True
            
        except Exception as e:
            self.logger.error(f"Error exporting learning data: {e}")
            return False
    
    def import_learning_data(self, file_path: str) -> bool:
        """Import learning data from backup."""
        try:
            import json
            
            with open(file_path, 'r') as f:
                import_data = json.load(f)
            
            # Convert string timestamps back to datetime objects
            def convert_datetime(obj):
                if isinstance(obj, str) and 'T' in obj:  # Likely ISO datetime string
                    try:
                        return datetime.fromisoformat(obj)
                    except:
                        return obj
                elif isinstance(obj, dict):
                    return {k: convert_datetime(v) for k, v in obj.items()}
                elif isinstance(obj, list):
                    return [convert_datetime(item) for item in obj]
                else:
                    return obj
            
            # Restore action memories
            self.action_memories = [FactoryActionMemory(**convert_datetime(m)) for m in import_data.get('action_memories', [])]
            
            # Restore niche profiles
            self.niche_profiles = {k: NicheLearningProfile(**convert_datetime(v)) for k, v in import_data.get('niche_profiles', {}).items()}
            
            # Restore lifecycle insights
            self.lifecycle_insights = {k: LifecycleStageInsights(**convert_datetime(v)) for k, v in import_data.get('lifecycle_insights', {}).items()}
            
            self.logger.info(f"Learning data imported from {file_path}")
            return True
            
        except Exception as e:
            self.logger.error(f"Error importing learning data: {e}")
            return False
