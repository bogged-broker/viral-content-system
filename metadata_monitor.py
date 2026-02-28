"""
metadata_monitor.py — MONITORING & HEALTH SYSTEMS (BLUEPRINT COMPLIANT)

PURPOSE:
Handle ALL monitoring, health checks, and metrics collection.
This file does NOT parse metadata - only monitors system health and performance.

SCALE TARGET: 10k-50k items/day
LATENCY TARGET: <50ms local, <200ms distributed
"""

import json
import time
import logging
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional
from dataclasses import dataclass, asdict
from threading import Lock
from collections import defaultdict

logger = logging.getLogger(__name__)

@dataclass
class HealthCheckResult:
    """Result of a health check"""
    check_name: str
    status: str  # "pass", "fail", "warn"
    message: str
    details: Optional[Dict[str, Any]] = None
    timestamp: str = ""
    
    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.utcnow().isoformat()

@dataclass
class SystemHealthStatus:
    """Overall system health status"""
    overall_health: str  # "healthy", "degraded", "critical"
    checks_performed: int
    checks_passed: int
    issues: List[HealthCheckResult]
    metrics: Dict[str, Any]
    timestamp: str = ""
    
    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.utcnow().isoformat()

class ProductionMetrics:
    """Thread-safe production metrics collector"""
    
    def __init__(self):
        self._lock = Lock()  # Thread safety for massive parallel batches
        self.items_processed = 0
        self.items_failed = 0
        self.total_processing_time = 0.0
        self.platform_counts = defaultdict(int)
        self.error_counts = defaultdict(int)
        self.start_time = time.time()
    
    def record_success(self, platform: str, processing_time_ms: float):
        """Record successful processing"""
        with self._lock:  # Atomic update
            self.items_processed += 1
            self.platform_counts[platform] += 1
            self.total_processing_time += processing_time_ms
    
    def record_failure(self, platform: str, error_code: str):
        """Record processing failure"""
        with self._lock:  # Atomic update
            self.items_failed += 1
            self.error_counts[error_code] += 1
            self.platform_counts[platform] += 1
    
    def get_summary(self) -> Dict[str, Any]:
        """Get metrics summary"""
        with self._lock:  # Atomic read
            runtime_seconds = time.time() - self.start_time
            return {
                'items_processed': self.items_processed,
                'items_failed': self.items_failed,
                'success_rate': self.items_processed / max(1, self.items_processed + self.items_failed),
                'avg_processing_time_ms': self.total_processing_time / max(1, self.items_processed),
                'items_per_second': self.items_processed / runtime_seconds,
                'platform_counts': dict(self.platform_counts),
                'error_counts': dict(self.error_counts),
                'runtime_seconds': runtime_seconds
            }
    
    def reset(self):
        """Reset all metrics"""
        with self._lock:
            self.items_processed = 0
            self.items_failed = 0
            self.total_processing_time = 0.0
            self.platform_counts.clear()
            self.error_counts.clear()
            self.start_time = time.time()

class MetadataHealthMonitor:
    """
    Health monitoring and metrics collection system.
    
    Responsibilities:
    1. System health validation
    2. Performance metrics collection
    3. Resource monitoring
    4. Issue detection and reporting
    
    This file NEVER parses or validates metadata - only monitors system health.
    """
    
    def __init__(self, data_root: Path):
        self.data_root = Path(data_root)
        
        # Required directories for health checks
        self.required_directories = [
            self.data_root / "raw" / "video",
            self.data_root / "raw" / "audio", 
            self.data_root / "raw" / "scrape",
            self.data_root / "processed" / "metadata"
        ]
        
        # Metrics collection
        self.metrics = ProductionMetrics()
        
        # Health check configuration
        self.health_check_timeout = 30.0  # seconds
        self.max_log_size_mb = 100  # Maximum log file size
        
    def validate_system_health(self) -> SystemHealthStatus:
        """Perform comprehensive system health validation"""
        issues = []
        checks_performed = 0
        checks_passed = 0
        
        # Check 1: Directory structure
        dir_result = self._check_directory_structure()
        issues.extend(dir_result.issues if hasattr(dir_result, 'issues') else [dir_result])
        checks_performed += 1
        if dir_result.status == "pass":
            checks_passed += 1
        
        # Check 2: Disk space
        space_result = self._check_disk_space()
        issues.extend(space_result.issues if hasattr(space_result, 'issues') else [space_result])
        checks_performed += 1
        if space_result.status == "pass":
            checks_passed += 1
        
        # Check 3: Log files
        log_result = self._check_log_files()
        issues.extend(log_result.issues if hasattr(log_result, 'issues') else [log_result])
        checks_performed += 1
        if log_result.status == "pass":
            checks_passed += 1
        
        # Check 4: Performance metrics
        perf_result = self._check_performance_metrics()
        issues.extend(perf_result.issues if hasattr(perf_result, 'issues') else [perf_result])
        checks_performed += 1
        if perf_result.status == "pass":
            checks_passed += 1
        
        # Check 5: Error rates
        error_result = self._check_error_rates()
        issues.extend(error_result.issues if hasattr(error_result, 'issues') else [error_result])
        checks_performed += 1
        if error_result.status == "pass":
            checks_passed += 1
        
        # Determine overall health
        overall_health = self._determine_overall_health(issues)
        
        return SystemHealthStatus(
            overall_health=overall_health,
            checks_performed=checks_performed,
            checks_passed=checks_passed,
            issues=issues,
            metrics=self.metrics.get_summary()
        )
    
    def _check_directory_structure(self) -> HealthCheckResult:
        """Check required directory structure"""
        missing_dirs = []
        
        for directory in self.required_directories:
            if not directory.exists():
                missing_dirs.append(str(directory))
        
        if missing_dirs:
            return HealthCheckResult(
                check_name="directory_structure",
                status="fail",
                message=f"Missing required directories: {len(missing_dirs)}",
                details={"missing_dirs": missing_dirs}
            )
        
        return HealthCheckResult(
            check_name="directory_structure",
            status="pass",
            message="All required directories exist"
        )
    
    def _check_disk_space(self) -> HealthCheckResult:
        """Check available disk space"""
        try:
            import shutil
            
            total, used, free = shutil.disk_usage(self.data_root)
            free_gb = free // (1024**3)
            total_gb = total // (1024**3)
            usage_percent = (used / total) * 100
            
            if free_gb < 1:  # Less than 1GB free
                return HealthCheckResult(
                    check_name="disk_space",
                    status="critical",
                    message=f"Critical: Only {free_gb}GB free ({usage_percent:.1f}% used)",
                    details={
                        "free_gb": free_gb,
                        "total_gb": total_gb,
                        "usage_percent": usage_percent
                    }
                )
            elif free_gb < 10:  # Less than 10GB free
                return HealthCheckResult(
                    check_name="disk_space",
                    status="warn",
                    message=f"Low disk space: {free_gb}GB free ({usage_percent:.1f}% used)",
                    details={
                        "free_gb": free_gb,
                        "total_gb": total_gb,
                        "usage_percent": usage_percent
                    }
                )
            else:
                return HealthCheckResult(
                    check_name="disk_space",
                    status="pass",
                    message=f"Sufficient disk space: {free_gb}GB free ({usage_percent:.1f}% used)"
                )
                
        except Exception as e:
            return HealthCheckResult(
                check_name="disk_space",
                status="fail",
                message=f"Failed to check disk space: {e}",
                details={"error": str(e)}
            )
    
    def _check_log_files(self) -> HealthCheckResult:
        """Check log file sizes and rotation"""
        try:
            log_files = [
                self.data_root / "metadata_parser.log",
                self.data_root / "metadata_parser_metrics.log"
            ]
            
            oversized_files = []
            for log_file in log_files:
                if log_file.exists():
                    size_mb = log_file.stat().st_size / (1024 * 1024)
                    if size_mb > self.max_log_size_mb:
                        oversized_files.append({
                            "file": str(log_file),
                            "size_mb": size_mb
                        })
            
            if oversized_files:
                return HealthCheckResult(
                    check_name="log_files",
                    status="warn",
                    message=f"Log files oversized: {len(oversized_files)} files",
                    details={"oversized_files": oversized_files}
                )
            
            return HealthCheckResult(
                check_name="log_files",
                status="pass",
                message="Log files within size limits"
            )
            
        except Exception as e:
            return HealthCheckResult(
                check_name="log_files",
                status="fail",
                message=f"Failed to check log files: {e}",
                details={"error": str(e)}
            )
    
    def _check_performance_metrics(self) -> HealthCheckResult:
        """Check performance metrics"""
        metrics = self.metrics.get_summary()
        
        # Check processing time
        avg_time = metrics['avg_processing_time_ms']
        if avg_time > 1000:  # More than 1 second average
            return HealthCheckResult(
                check_name="performance_metrics",
                status="warn",
                message=f"Slow processing: {avg_time:.2f}ms average",
                details={"avg_processing_time_ms": avg_time}
            )
        
        # Check success rate
        success_rate = metrics['success_rate']
        if success_rate < 0.9:  # Less than 90% success rate
            return HealthCheckResult(
                check_name="performance_metrics",
                status="warn",
                message=f"Low success rate: {success_rate:.2%}",
                details={"success_rate": success_rate}
            )
        
        return HealthCheckResult(
            check_name="performance_metrics",
            status="pass",
            message=f"Performance acceptable: {avg_time:.2f}ms avg, {success_rate:.2%} success"
        )
    
    def _check_error_rates(self) -> HealthCheckResult:
        """Check error rates and patterns"""
        metrics = self.metrics.get_summary()
        
        total_items = metrics['items_processed'] + metrics['items_failed']
        if total_items == 0:
            return HealthCheckResult(
                check_name="error_rates",
                status="pass",
                message="No items processed yet"
            )
        
        error_rate = metrics['items_failed'] / total_items
        if error_rate > 0.1:  # More than 10% error rate
            return HealthCheckResult(
                check_name="error_rates",
                status="warn",
                message=f"High error rate: {error_rate:.2%}",
                details={
                    "error_rate": error_rate,
                    "error_counts": metrics['error_counts']
                }
            )
        
        return HealthCheckResult(
            check_name="error_rates",
            status="pass",
            message=f"Error rate acceptable: {error_rate:.2%}"
        )
    
    def _determine_overall_health(self, issues: List[HealthCheckResult]) -> str:
        """Determine overall system health based on issues"""
        if not issues:
            return "healthy"
        
        critical_count = sum(1 for issue in issues if issue.status == "critical")
        fail_count = sum(1 for issue in issues if issue.status == "fail")
        warn_count = sum(1 for issue in issues if issue.status == "warn")
        
        if critical_count > 0:
            return "critical"
        elif fail_count > 0:
            return "degraded"
        elif warn_count > 2:  # More than 2 warnings
            return "degraded"
        else:
            return "healthy"
    
    def get_production_metrics(self) -> Dict[str, Any]:
        """Get current production metrics"""
        return self.metrics.get_summary()
    
    def reset_metrics(self):
        """Reset production metrics"""
        self.metrics.reset()
        logger.info("Production metrics reset")

# Global metrics instance (for backward compatibility)
production_metrics = ProductionMetrics()
