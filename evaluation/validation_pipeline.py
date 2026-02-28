"""
/models/evaluation/validation_pipeline.py

Model Integrity, Causality & Safety Validation Layer

This file answers ONE question:
"Is this model's behavior still valid, stable, causal, and safe 
compared to what it was trained and authorized to do?"

CORE PRINCIPLE: Performance without integrity is a system failure.
Validation ≠ accuracy. Validation = fitness to continue existing.

NO model trains, updates, or deploys without passing this file.
"""

import numpy as np
import pandas as pd
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass, asdict
from datetime import datetime
from enum import Enum
import logging
from scipy import stats
from sklearn.calibration import calibration_curve

logger = logging.getLogger(__name__)


class Verdict(Enum):
    """Non-overridable verdict states"""
    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"


@dataclass
class ValidationResult:
    """Machine-verifiable validation output schema - LOCKED"""
    model_id: str
    model_version: str
    verdict: Verdict
    confidence: float  # 0.0-1.0
    violations: List[str]
    metrics: Dict[str, Any]
    safe_for_training: bool
    safe_for_deployment: bool
    timestamp: str
    
    def to_dict(self) -> Dict:
        result = asdict(self)
        result['verdict'] = self.verdict.value
        return result


@dataclass
class ValidationConfig:
    """Validation thresholds and parameters"""
    # Drift detection
    drift_magnitude_warn: float = 0.15
    drift_magnitude_fail: float = 0.30
    
    # Calibration
    calibration_error_warn: float = 0.10
    calibration_error_fail: float = 0.20
    
    # Causal invariants
    invariant_violation_threshold: float = 0.05
    
    # Uncertainty
    min_uncertainty_low_data: float = 0.20
    uncertainty_collapse_threshold: float = 0.05
    
    # Stress test
    stress_test_failure_rate_max: float = 0.15


class SchemaValidator:
    """Ensures structural integrity of model outputs"""
    
    REQUIRED_FIELDS = [
        'prediction',
        'uncertainty',
        'horizon',
        'timestamp',
        'confidence_interval'
    ]
    
    def validate(self, outputs: pd.DataFrame) -> Tuple[bool, List[str]]:
        """
        Returns: (is_valid, violations)
        Failure here = immediate FAIL
        """
        violations = []
        
        # Check required fields
        missing = set(self.REQUIRED_FIELDS) - set(outputs.columns)
        if missing:
            violations.append(f"Missing required fields: {missing}")
        
        # Check for NaN/inf
        if outputs.isnull().any().any():
            violations.append("NaN values detected in outputs")
        
        if np.isinf(outputs.select_dtypes(include=[np.number])).any().any():
            violations.append("Inf values detected in outputs")
        
        # Check monotonic time assumptions
        if 'timestamp' in outputs.columns:
            if not outputs['timestamp'].is_monotonic_increasing:
                violations.append("Timestamp monotonicity violated")
        
        # Verify horizon formats
        if 'horizon' in outputs.columns:
            invalid_horizons = outputs[outputs['horizon'] <= 0]
            if len(invalid_horizons) > 0:
                violations.append(f"Invalid horizon values: {len(invalid_horizons)} records")
        
        return len(violations) == 0, violations


class LeakageDetector:
    """
    CRITICAL: Detects future information leakage
    Any leakage → automatic FAIL + training freeze
    """
    
    def detect(
        self, 
        outputs: pd.DataFrame,
        video_metadata: pd.DataFrame,
        current_time: datetime
    ) -> Tuple[bool, List[str]]:
        """
        Returns: (no_leakage, violations)
        """
        violations = []
        
        # Check for future timestamps
        if 'timestamp' in outputs.columns:
            future_preds = outputs[outputs['timestamp'] > current_time]
            if len(future_preds) > 0:
                violations.append(
                    f"CRITICAL: {len(future_preds)} predictions from future timestamps"
                )
        
        # Check engagement windows vs video age
        if all(col in outputs.columns for col in ['video_id', 'horizon']):
            merged = outputs.merge(video_metadata[['video_id', 'upload_time']], on='video_id')
            merged['video_age'] = (current_time - merged['upload_time']).dt.total_seconds() / 3600
            
            invalid = merged[merged['horizon'] > merged['video_age']]
            if len(invalid) > 0:
                violations.append(
                    f"CRITICAL: {len(invalid)} predictions with horizon exceeding video age"
                )
        
        # Check for label contamination (engagement data in features)
        suspicious_cols = [col for col in outputs.columns 
                          if any(term in col.lower() 
                                for term in ['views', 'likes', 'shares', 'engagement'])]
        if suspicious_cols:
            violations.append(
                f"WARNING: Suspicious feature names suggesting label leakage: {suspicious_cols}"
            )
        
        return len(violations) == 0, violations


class DriftDetector:
    """Detects input distribution and prediction surface drift"""
    
    def __init__(self, config: ValidationConfig):
        self.config = config
    
    def detect(
        self,
        current_outputs: pd.DataFrame,
        baseline_outputs: pd.DataFrame
    ) -> Tuple[Verdict, List[str], Dict[str, float]]:
        """
        Returns: (verdict, violations, metrics)
        """
        violations = []
        metrics = {}
        
        # Input distribution drift (KL divergence on predictions)
        current_dist = current_outputs['prediction'].values
        baseline_dist = baseline_outputs['prediction'].values
        
        # Normalize to distributions
        current_hist, bins = np.histogram(current_dist, bins=50, density=True)
        baseline_hist, _ = np.histogram(baseline_dist, bins=bins, density=True)
        
        # Add small epsilon to avoid log(0)
        eps = 1e-10
        current_hist = current_hist + eps
        baseline_hist = baseline_hist + eps
        
        # KL divergence
        kl_div = np.sum(current_hist * np.log(current_hist / baseline_hist))
        metrics['kl_divergence'] = float(kl_div)
        
        # Wasserstein distance (earth mover's distance)
        wasserstein_dist = stats.wasserstein_distance(current_dist, baseline_dist)
        metrics['wasserstein_distance'] = float(wasserstein_dist)
        
        # Embedding drift (if available)
        if 'embedding_mean' in current_outputs.columns:
            current_emb = current_outputs['embedding_mean'].values
            baseline_emb = baseline_outputs['embedding_mean'].values
            emb_drift = np.linalg.norm(current_emb.mean() - baseline_emb.mean())
            metrics['embedding_drift'] = float(emb_drift)
        
        # Determine verdict
        max_drift = max(kl_div, wasserstein_dist)
        
        if max_drift > self.config.drift_magnitude_fail:
            violations.append(f"Severe drift detected: {max_drift:.4f}")
            return Verdict.FAIL, violations, metrics
        elif max_drift > self.config.drift_magnitude_warn:
            violations.append(f"Moderate drift detected: {max_drift:.4f}")
            return Verdict.WARN, violations, metrics
        
        return Verdict.PASS, violations, metrics


class CalibrationAuditor:
    """
    Checks calibration integrity
    Uncalibrated predictors destroy RL agents
    """
    
    def __init__(self, config: ValidationConfig):
        self.config = config
    
    def audit(
        self,
        predictions: np.ndarray,
        actuals: np.ndarray,
        confidences: np.ndarray
    ) -> Tuple[Verdict, List[str], Dict[str, float]]:
        """
        Returns: (verdict, violations, metrics)
        """
        violations = []
        metrics = {}
        
        # Expected Calibration Error (ECE)
        try:
            prob_true, prob_pred = calibration_curve(
                actuals > np.median(actuals),  # Binary outcome
                confidences,
                n_bins=10,
                strategy='uniform'
            )
            ece = np.mean(np.abs(prob_true - prob_pred))
            metrics['expected_calibration_error'] = float(ece)
        except Exception as e:
            violations.append(f"Calibration calculation failed: {str(e)}")
            ece = 1.0
        
        # Confidence interval coverage
        pred_lower = predictions - confidences * predictions
        pred_upper = predictions + confidences * predictions
        
        coverage = np.mean((actuals >= pred_lower) & (actuals <= pred_upper))
        metrics['confidence_coverage'] = float(coverage)
        
        # Expected coverage for 68% confidence interval (1 sigma)
        expected_coverage = 0.68
        coverage_error = abs(coverage - expected_coverage)
        metrics['coverage_error'] = float(coverage_error)
        
        # Check for overconfidence creep
        avg_confidence = np.mean(confidences)
        metrics['avg_confidence'] = float(avg_confidence)
        
        if avg_confidence > 0.95:
            violations.append(f"Overconfidence detected: {avg_confidence:.4f}")
        
        # Determine verdict
        if ece > self.config.calibration_error_fail:
            violations.append(f"Calibration severely degraded: ECE={ece:.4f}")
            return Verdict.FAIL, violations, metrics
        elif ece > self.config.calibration_error_warn:
            violations.append(f"Calibration degraded: ECE={ece:.4f}")
            return Verdict.WARN, violations, metrics
        
        return Verdict.PASS, violations, metrics


class CausalInvariantChecker:
    """
    VERY IMPORTANT: Asserts causal invariants
    If violated → model is not causal-safe
    """
    
    def __init__(self, config: ValidationConfig):
        self.config = config
    
    def check(
        self,
        outputs: pd.DataFrame,
        metadata: pd.DataFrame
    ) -> Tuple[bool, List[str], Dict[str, float]]:
        """
        Returns: (invariants_hold, violations, metrics)
        """
        violations = []
        metrics = {}
        
        # Invariant 1: Engagement velocity ↔ views monotonicity
        if all(col in outputs.columns for col in ['engagement_velocity', 'predicted_views']):
            corr = outputs['engagement_velocity'].corr(outputs['predicted_views'])
            metrics['velocity_views_correlation'] = float(corr)
            
            if corr < 0.5:  # Should be strongly positive
                violations.append(
                    f"Velocity-views monotonicity violated: correlation={corr:.4f}"
                )
        
        # Invariant 2: Uncertainty ↑ when signal ↓
        if all(col in outputs.columns for col in ['uncertainty', 'signal_strength']):
            # Should be negative correlation
            corr = outputs['uncertainty'].corr(outputs['signal_strength'])
            metrics['uncertainty_signal_correlation'] = float(corr)
            
            if corr > -0.3:  # Should be negative
                violations.append(
                    f"Uncertainty-signal invariant violated: correlation={corr:.4f}"
                )
        
        # Invariant 3: Cold-start confidence widening
        if 'video_age_hours' in metadata.columns:
            merged = outputs.merge(metadata[['video_id', 'video_age_hours']], on='video_id')
            
            cold_start = merged[merged['video_age_hours'] < 1]
            mature = merged[merged['video_age_hours'] > 24]
            
            if len(cold_start) > 0 and len(mature) > 0:
                cold_unc = cold_start['uncertainty'].mean()
                mature_unc = mature['uncertainty'].mean()
                
                metrics['cold_start_uncertainty'] = float(cold_unc)
                metrics['mature_uncertainty'] = float(mature_unc)
                
                if cold_unc <= mature_unc:
                    violations.append(
                        f"Cold-start uncertainty not higher than mature: "
                        f"{cold_unc:.4f} vs {mature_unc:.4f}"
                    )
        
        # Invariant 4: Prediction bounds widen with horizon
        if 'horizon' in outputs.columns and 'confidence_interval_width' in outputs.columns:
            corr = outputs['horizon'].corr(outputs['confidence_interval_width'])
            metrics['horizon_uncertainty_correlation'] = float(corr)
            
            if corr < 0.3:  # Should be positive
                violations.append(
                    f"Horizon-uncertainty invariant violated: correlation={corr:.4f}"
                )
        
        invariants_hold = len(violations) == 0
        return invariants_hold, violations, metrics


class UncertaintyAuditor:
    """
    Ensures uncertainty estimates are healthy
    High confidence + low data = FAIL
    """
    
    def __init__(self, config: ValidationConfig):
        self.config = config
    
    def audit(
        self,
        outputs: pd.DataFrame,
        metadata: pd.DataFrame
    ) -> Tuple[Verdict, List[str], Dict[str, float]]:
        """
        Returns: (verdict, violations, metrics)
        """
        violations = []
        metrics = {}
        
        # Check epistemic vs aleatoric separation (if available)
        if all(col in outputs.columns for col in ['epistemic_uncertainty', 'aleatoric_uncertainty']):
            ep_mean = outputs['epistemic_uncertainty'].mean()
            al_mean = outputs['aleatoric_uncertainty'].mean()
            
            metrics['epistemic_mean'] = float(ep_mean)
            metrics['aleatoric_mean'] = float(al_mean)
            
            # Epistemic should dominate in low-data regime
            low_data = outputs[outputs['data_points'] < 100] if 'data_points' in outputs.columns else outputs[:100]
            if len(low_data) > 0:
                ep_low = low_data['epistemic_uncertainty'].mean()
                al_low = low_data['aleatoric_uncertainty'].mean()
                
                if ep_low < al_low:
                    violations.append(
                        "Epistemic uncertainty too low in low-data regime"
                    )
        
        # Check uncertainty correlates with ambiguity
        if 'uncertainty' in outputs.columns and 'prediction' in outputs.columns:
            # Group by prediction bins
            pred_bins = pd.qcut(outputs['prediction'], q=10, duplicates='drop')
            uncertainty_by_bin = outputs.groupby(pred_bins)['uncertainty'].std()
            
            metrics['uncertainty_variability'] = float(uncertainty_by_bin.mean())
        
        # Check for uncertainty collapse under low data
        if 'data_points' in outputs.columns:
            low_data_mask = outputs['data_points'] < 50
            if low_data_mask.sum() > 0:
                low_data_unc = outputs.loc[low_data_mask, 'uncertainty'].mean()
                
                if low_data_unc < self.config.min_uncertainty_low_data:
                    violations.append(
                        f"Uncertainty collapsed under low data: {low_data_unc:.4f}"
                    )
                    return Verdict.FAIL, violations, metrics
        
        # Check for overall uncertainty collapse
        if 'uncertainty' in outputs.columns:
            avg_unc = outputs['uncertainty'].mean()
            metrics['avg_uncertainty'] = float(avg_unc)
            
            if avg_unc < self.config.uncertainty_collapse_threshold:
                violations.append(f"Overall uncertainty collapse: {avg_unc:.4f}")
                return Verdict.FAIL, violations, metrics
        
        return Verdict.PASS, violations, metrics


class StressTestRunner:
    """
    Tests behavioral sanity under adversarial conditions
    NOT testing accuracy - testing behavioral coherence
    """
    
    def __init__(self, config: ValidationConfig):
        self.config = config
    
    def run(
        self,
        model_predictor,  # Callable that takes inputs and returns predictions
        baseline_inputs: pd.DataFrame
    ) -> Tuple[Verdict, List[str], Dict[str, float]]:
        """
        Returns: (verdict, violations, metrics)
        """
        violations = []
        metrics = {}
        failures = 0
        total_tests = 0
        
        # Test 1: Low-signal simulations
        low_signal_inputs = baseline_inputs.copy()
        if 'signal_strength' in low_signal_inputs.columns:
            low_signal_inputs['signal_strength'] = 0.01
            
            try:
                predictions = model_predictor(low_signal_inputs)
                
                # Should have high uncertainty
                if 'uncertainty' in predictions.columns:
                    avg_unc = predictions['uncertainty'].mean()
                    if avg_unc < 0.3:
                        failures += 1
                        violations.append("Low uncertainty under low signal")
                
                total_tests += 1
            except Exception as e:
                failures += 1
                violations.append(f"Low-signal test crashed: {str(e)}")
                total_tests += 1
        
        # Test 2: Adversarial noise injection
        noisy_inputs = baseline_inputs.copy()
        numeric_cols = noisy_inputs.select_dtypes(include=[np.number]).columns
        
        for col in numeric_cols[:5]:  # Test subset to avoid excessive compute
            noise = np.random.normal(0, 0.1 * noisy_inputs[col].std(), len(noisy_inputs))
            noisy_inputs[col] = noisy_inputs[col] + noise
        
        try:
            clean_preds = model_predictor(baseline_inputs)
            noisy_preds = model_predictor(noisy_inputs)
            
            # Predictions should not change drastically
            pred_diff = np.abs(clean_preds['prediction'].values - noisy_preds['prediction'].values)
            avg_diff = pred_diff.mean()
            
            metrics['noise_sensitivity'] = float(avg_diff)
            
            if avg_diff > 0.5 * clean_preds['prediction'].std():
                failures += 1
                violations.append(f"Excessive noise sensitivity: {avg_diff:.4f}")
            
            total_tests += 1
        except Exception as e:
            failures += 1
            violations.append(f"Noise injection test crashed: {str(e)}")
            total_tests += 1
        
        # Test 3: Boundary condition inputs
        boundary_inputs = baseline_inputs.copy()
        
        # Set to extreme but valid values
        for col in numeric_cols[:3]:
            boundary_inputs.loc[:len(boundary_inputs)//2, col] = baseline_inputs[col].min()
            boundary_inputs.loc[len(boundary_inputs)//2:, col] = baseline_inputs[col].max()
        
        try:
            boundary_preds = model_predictor(boundary_inputs)
            
            # Should not produce NaN/Inf
            if boundary_preds.isnull().any().any():
                failures += 1
                violations.append("NaN outputs on boundary conditions")
            
            total_tests += 1
        except Exception as e:
            failures += 1
            violations.append(f"Boundary test crashed: {str(e)}")
            total_tests += 1
        
        # Calculate failure rate
        failure_rate = failures / max(total_tests, 1)
        metrics['stress_test_failure_rate'] = failure_rate
        metrics['stress_tests_run'] = total_tests
        
        # Determine verdict
        if failure_rate > self.config.stress_test_failure_rate_max:
            return Verdict.FAIL, violations, metrics
        elif failures > 0:
            return Verdict.WARN, violations, metrics
        
        return Verdict.PASS, violations, metrics


class VerdictEmitter:
    """
    Aggregates all validation results and emits final verdict
    No overrides allowed
    """
    
    @staticmethod
    def emit(
        model_id: str,
        model_version: str,
        all_verdicts: List[Verdict],
        all_violations: List[str],
        all_metrics: Dict[str, Any]
    ) -> ValidationResult:
        """
        Emission rules (LOCKED):
        - Leakage detected → FAIL
        - Causal invariant broken → FAIL
        - Calibration degraded → WARN
        - Mild drift → WARN
        - All checks clean → PASS
        """
        
        # Aggregate verdict (most severe wins)
        if Verdict.FAIL in all_verdicts:
            final_verdict = Verdict.FAIL
        elif Verdict.WARN in all_verdicts:
            final_verdict = Verdict.WARN
        else:
            final_verdict = Verdict.PASS
        
        # Calculate confidence (inverse of violation severity)
        confidence = 1.0 - (len(all_violations) / max(len(all_verdicts) * 3, 1))
        confidence = max(0.0, min(1.0, confidence))
        
        # Determine safety flags
        safe_for_training = final_verdict != Verdict.FAIL
        safe_for_deployment = final_verdict == Verdict.PASS
        
        return ValidationResult(
            model_id=model_id,
            model_version=model_version,
            verdict=final_verdict,
            confidence=confidence,
            violations=all_violations,
            metrics=all_metrics,
            safe_for_training=safe_for_training,
            safe_for_deployment=safe_for_deployment,
            timestamp=datetime.utcnow().isoformat()
        )


class ValidationPipeline:
    """
    Main validation orchestrator
    
    GATE: No model trains, updates, or deploys without passing this pipeline
    """
    
    def __init__(self, config: Optional[ValidationConfig] = None):
        self.config = config or ValidationConfig()
        
        # Initialize validators
        self.schema_validator = SchemaValidator()
        self.leakage_detector = LeakageDetector()
        self.drift_detector = DriftDetector(self.config)
        self.calibration_auditor = CalibrationAuditor(self.config)
        self.causal_checker = CausalInvariantChecker(self.config)
        self.uncertainty_auditor = UncertaintyAuditor(self.config)
        self.stress_tester = StressTestRunner(self.config)
        self.verdict_emitter = VerdictEmitter()
    
    def validate(
        self,
        model_id: str,
        model_version: str,
        current_outputs: pd.DataFrame,
        baseline_outputs: pd.DataFrame,
        video_metadata: pd.DataFrame,
        actuals: Optional[np.ndarray] = None,
        model_predictor: Optional[callable] = None
    ) -> ValidationResult:
        """
        Main validation entry point
        
        Args:
            model_id: Unique model identifier
            model_version: Model version string
            current_outputs: Current model predictions with uncertainty
            baseline_outputs: Last known good predictions
            video_metadata: Video metadata including upload times
            actuals: Actual outcomes (for calibration check)
            model_predictor: Callable for stress testing
        
        Returns:
            ValidationResult with verdict and detailed metrics
        """
        
        logger.info(f"Starting validation for {model_id} v{model_version}")
        
        all_verdicts = []
        all_violations = []
        all_metrics = {}
        
        # 1. Schema Validation (CRITICAL - immediate fail)
        logger.info("Running schema validation...")
        schema_valid, schema_violations = self.schema_validator.validate(current_outputs)
        
        if not schema_valid:
            logger.error(f"Schema validation FAILED: {schema_violations}")
            return self.verdict_emitter.emit(
                model_id=model_id,
                model_version=model_version,
                all_verdicts=[Verdict.FAIL],
                all_violations=schema_violations,
                all_metrics={'schema_validation': 'FAILED'}
            )
        
        all_metrics['schema_validation'] = 'PASSED'
        
        # 2. Leakage Detection (CRITICAL - immediate fail + training freeze)
        logger.info("Running leakage detection...")
        no_leakage, leakage_violations = self.leakage_detector.detect(
            outputs=current_outputs,
            video_metadata=video_metadata,
            current_time=datetime.utcnow()
        )
        
        if not no_leakage:
            logger.error(f"LEAKAGE DETECTED: {leakage_violations}")
            all_violations.extend(leakage_violations)
            return self.verdict_emitter.emit(
                model_id=model_id,
                model_version=model_version,
                all_verdicts=[Verdict.FAIL],
                all_violations=all_violations,
                all_metrics={'leakage_detected': True}
            )
        
        all_metrics['leakage_detected'] = False
        
        # 3. Drift Detection
        logger.info("Running drift detection...")
        drift_verdict, drift_violations, drift_metrics = self.drift_detector.detect(
            current_outputs=current_outputs,
            baseline_outputs=baseline_outputs
        )
        
        all_verdicts.append(drift_verdict)
        all_violations.extend(drift_violations)
        all_metrics.update(drift_metrics)
        
        # 4. Calibration Audit (if actuals available)
        if actuals is not None and 'prediction' in current_outputs.columns:
            logger.info("Running calibration audit...")
            
            predictions = current_outputs['prediction'].values
            confidences = current_outputs.get('uncertainty', np.ones_like(predictions) * 0.5).values
            
            cal_verdict, cal_violations, cal_metrics = self.calibration_auditor.audit(
                predictions=predictions,
                actuals=actuals,
                confidences=confidences
            )
            
            all_verdicts.append(cal_verdict)
            all_violations.extend(cal_violations)
            all_metrics.update(cal_metrics)
        
        # 5. Causal Invariant Checks (CRITICAL)
        logger.info("Running causal invariant checks...")
        invariants_hold, inv_violations, inv_metrics = self.causal_checker.check(
            outputs=current_outputs,
            metadata=video_metadata
        )
        
        if not invariants_hold:
            logger.error(f"CAUSAL INVARIANTS VIOLATED: {inv_violations}")
            all_violations.extend(inv_violations)
            all_verdicts.append(Verdict.FAIL)
        
        all_metrics.update(inv_metrics)
        
        # 6. Uncertainty Audit
        logger.info("Running uncertainty audit...")
        unc_verdict, unc_violations, unc_metrics = self.uncertainty_auditor.audit(
            outputs=current_outputs,
            metadata=video_metadata
        )
        
        all_verdicts.append(unc_verdict)
        all_violations.extend(unc_violations)
        all_metrics.update(unc_metrics)
        
        # 7. Stress Testing (if predictor available)
        if model_predictor is not None:
            logger.info("Running stress tests...")
            stress_verdict, stress_violations, stress_metrics = self.stress_tester.run(
                model_predictor=model_predictor,
                baseline_inputs=baseline_outputs
            )
            
            all_verdicts.append(stress_verdict)
            all_violations.extend(stress_violations)
            all_metrics.update(stress_metrics)
        
        # 8. Emit Final Verdict
        logger.info("Emitting final verdict...")
        result = self.verdict_emitter.emit(
            model_id=model_id,
            model_version=model_version,
            all_verdicts=all_verdicts,
            all_violations=all_violations,
            all_metrics=all_metrics
        )
        
        logger.info(
            f"Validation complete: {result.verdict.value} "
            f"(confidence: {result.confidence:.3f}, "
            f"violations: {len(result.violations)})"
        )
        
        return result
    
    def validate_deterministic(
        self,
        model_id: str,
        model_version: str,
        current_outputs: pd.DataFrame,
        baseline_outputs: pd.DataFrame,
        video_metadata: pd.DataFrame,
        seed: int = 42
    ) -> ValidationResult:
        """
        Deterministic validation for audits and replays
        Same inputs ⇒ same verdict (MANDATORY for legal safety)
        """
        np.random.seed(seed)
        
        return self.validate(
            model_id=model_id,
            model_version=model_version,
            current_outputs=current_outputs,
            baseline_outputs=baseline_outputs,
            video_metadata=video_metadata
        )


# Example usage
if __name__ == "__main__":
    # Configure logging
    logging.basicConfig(level=logging.INFO)
    
    # Initialize pipeline
    config = ValidationConfig(
        drift_magnitude_warn=0.15,
        drift_magnitude_fail=0.30,
        calibration_error_warn=0.10
    )
    
    pipeline = ValidationPipeline(config)
    
    # Mock data for demonstration
    current_outputs = pd.DataFrame({
        'video_id': range(100),
        'prediction': np.random.lognormal(10, 2, 100),
        'uncertainty': np.random.uniform(0.1, 0.4, 100),
        'horizon': np.random.choice([1, 6, 24, 168], 100),
        'timestamp': pd.date_range('2025-01-01', periods=100, freq='h'),
        'confidence_interval': np.random.uniform(0.15, 0.35, 100),
        'engagement_velocity': np.random.uniform(0, 100, 100),
        'predicted_views': np.random.lognormal(10, 2, 100),
        'signal_strength': np.random.uniform(0.3, 1.0, 100)
    })
    
    baseline_outputs = current_outputs.copy()
    baseline_outputs['prediction'] = baseline_outputs['prediction'] * np.random.uniform(0.95, 1.05, 100)
    
    video_metadata = pd.DataFrame({
        'video_id': range(100),
        'upload_time': pd.date_range('2025-01-01', periods=100, freq='h') - pd.Timedelta(hours=2),
        'video_age_hours': np.random.uniform(1, 72, 100)
    })
    
    # Run