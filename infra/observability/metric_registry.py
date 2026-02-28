"""
Metric Registry
Central registry for all system metrics with Prometheus-compatible definitions
"""

from typing import Dict, List, Optional
from dataclasses import dataclass
from enum import Enum

from infra.observability.metrics_collector import (
    MetricType,
    MetricScope,
    AggregationType
)


@dataclass(frozen=True)
class MetricDefinition:
    """
    Definition of a metric with all metadata.
    """
    name: str
    description: str
    metric_type: MetricType
    scope: MetricScope
    aggregation: AggregationType
    unit: str
    labels: List[str]
    
    def to_prometheus_help(self) -> str:
        """Generate Prometheus HELP comment."""
        return f"# HELP {self.name} {self.description}"
    
    def to_prometheus_type(self) -> str:
        """Generate Prometheus TYPE comment."""
        prom_type = {
            MetricType.COUNTER: "counter",
            MetricType.GAUGE: "gauge",
            MetricType.HISTOGRAM: "histogram",
            MetricType.RATE: "gauge"
        }.get(self.metric_type, "untyped")
        return f"# TYPE {self.name} {prom_type}"


class MetricRegistry:
    """
    Central registry for all system metrics.
    
    All metrics must be registered here before use.
    This ensures:
    - No duplicate metric names
    - Consistent labeling
    - Documentation
    - Type safety
    """
    
    def __init__(self):
        self._metrics: Dict[str, MetricDefinition] = {}
        self._register_core_metrics()
    
    def register(self, definition: MetricDefinition) -> None:
        """
        Register a metric definition.
        
        Raises:
            ValueError: If metric already registered
        """
        if definition.name in self._metrics:
            raise ValueError(
                f"Metric already registered: {definition.name}"
            )
        self._metrics[definition.name] = definition
    
    def get(self, name: str) -> Optional[MetricDefinition]:
        """Get metric definition by name."""
        return self._metrics.get(name)
    
    def list_all(self) -> List[MetricDefinition]:
        """List all registered metrics."""
        return list(self._metrics.values())
    
    def _register_core_metrics(self):
        """Register core system metrics."""
        
        # System metrics
        self.register(MetricDefinition(
            name="viral_system_requests_total",
            description="Total number of system requests",
            metric_type=MetricType.COUNTER,
            scope=MetricScope.SYSTEM,
            aggregation=AggregationType.SUM,
            unit="requests",
            labels=["method", "endpoint", "status"]
        ))
        
        self.register(MetricDefinition(
            name="viral_system_errors_total",
            description="Total number of system errors",
            metric_type=MetricType.COUNTER,
            scope=MetricScope.SYSTEM,
            aggregation=AggregationType.SUM,
            unit="errors",
            labels=["error_type", "component"]
        ))
        
        self.register(MetricDefinition(
            name="viral_system_components_active",
            description="Number of active system components",
            metric_type=MetricType.GAUGE,
            scope=MetricScope.SYSTEM,
            aggregation=AggregationType.COUNT,
            unit="components",
            labels=["component"]
        ))
        
        # Ingestion metrics
        self.register(MetricDefinition(
            name="viral_ingestion_items_processed_total",
            description="Total number of items processed by ingestion pipeline",
            metric_type=MetricType.COUNTER,
            scope=MetricScope.BUSINESS,
            aggregation=AggregationType.SUM,
            unit="items",
            labels=["platform", "status"]
        ))
        
        self.register(MetricDefinition(
            name="viral_ingestion_duration_seconds",
            description="Time taken to process ingestion items",
            metric_type=MetricType.HISTOGRAM,
            scope=MetricScope.SYSTEM,
            aggregation=AggregationType.P95,
            unit="seconds",
            labels=["platform"]
        ))
        
        # Generation metrics
        self.register(MetricDefinition(
            name="viral_generation_content_created_total",
            description="Total number of content items created",
            metric_type=MetricType.COUNTER,
            scope=MetricScope.BUSINESS,
            aggregation=AggregationType.SUM,
            unit="items",
            labels=["content_type", "status"]
        ))
        
        self.register(MetricDefinition(
            name="viral_generation_duration_seconds",
            description="Time taken to generate content",
            metric_type=MetricType.HISTOGRAM,
            scope=MetricScope.SYSTEM,
            aggregation=AggregationType.P95,
            unit="seconds",
            labels=["content_type"]
        ))
        
        # Scoring metrics
        self.register(MetricDefinition(
            name="viral_scoring_scores_computed_total",
            description="Total number of viral scores computed",
            metric_type=MetricType.COUNTER,
            scope=MetricScope.BUSINESS,
            aggregation=AggregationType.SUM,
            unit="scores",
            labels=["score_type"]
        ))
        
        self.register(MetricDefinition(
            name="viral_scoring_score_value",
            description="Viral score value distribution",
            metric_type=MetricType.HISTOGRAM,
            scope=MetricScope.BUSINESS,
            aggregation=AggregationType.MEAN,
            unit="score",
            labels=["score_type"]
        ))
        
        # Posting metrics
        self.register(MetricDefinition(
            name="viral_posting_posts_sent_total",
            description="Total number of posts sent",
            metric_type=MetricType.COUNTER,
            scope=MetricScope.BUSINESS,
            aggregation=AggregationType.SUM,
            unit="posts",
            labels=["platform", "status"]
        ))
        
        # Pipeline metrics
        self.register(MetricDefinition(
            name="viral_pipeline_queue_depth",
            description="Current queue depth for pipelines",
            metric_type=MetricType.GAUGE,
            scope=MetricScope.SYSTEM,
            aggregation=AggregationType.COUNT,
            unit="items",
            labels=["pipeline"]
        ))
        
        self.register(MetricDefinition(
            name="viral_pipeline_duration_seconds",
            description="Pipeline execution duration",
            metric_type=MetricType.HISTOGRAM,
            scope=MetricScope.SYSTEM,
            aggregation=AggregationType.P95,
            unit="seconds",
            labels=["pipeline", "stage"]
        ))
        
        self.register(MetricDefinition(
            name="viral_pipeline_errors_total",
            description="Total pipeline errors",
            metric_type=MetricType.COUNTER,
            scope=MetricScope.SYSTEM,
            aggregation=AggregationType.SUM,
            unit="errors",
            labels=["pipeline", "error_type"]
        ))
        
        # Safety metrics
        self.register(MetricDefinition(
            name="viral_safety_violations_total",
            description="Total safety violations detected",
            metric_type=MetricType.COUNTER,
            scope=MetricScope.SAFETY,
            aggregation=AggregationType.SUM,
            unit="violations",
            labels=["violation_type", "severity"]
        ))
        
        self.register(MetricDefinition(
            name="viral_safety_trust_score",
            description="Current trust score",
            metric_type=MetricType.GAUGE,
            scope=MetricScope.SAFETY,
            aggregation=AggregationType.MEAN,
            unit="score",
            labels=["account_id"]
        ))


# Global registry instance
_metric_registry: Optional[MetricRegistry] = None


def get_metric_registry() -> MetricRegistry:
    """Get or create global metric registry."""
    global _metric_registry
    if _metric_registry is None:
        _metric_registry = MetricRegistry()
    return _metric_registry


__all__ = [
    'MetricDefinition',
    'MetricRegistry',
    'get_metric_registry',
]
