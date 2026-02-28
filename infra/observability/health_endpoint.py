"""
Health Report Endpoint
Provides HTTP endpoint for health checks and metrics exposure
"""

import asyncio
import json
import logging
from typing import Dict, Any, Optional
from datetime import datetime
from aiohttp import web
from aiohttp.web import Response

from infra.observability.metrics_collector import MetricsCollector, get_metrics_collector
from infra.observability.health_checks import HealthChecker, get_health_checker


class HealthEndpoint:
    """
    HTTP endpoint for health checks and metrics.
    
    Provides:
    - /health: Basic health check
    - /health/detailed: Detailed health status
    - /health/metrics: Prometheus-formatted metrics
    - /metrics: Prometheus metrics (alias)
    """
    
    def __init__(
        self,
        metrics_collector: Optional[MetricsCollector] = None,
        health_checker: Optional[HealthChecker] = None,
        port: int = 8000
    ):
        self.logger = logging.getLogger(__name__)
        self.metrics_collector = metrics_collector or get_metrics_collector()
        self.health_checker = health_checker or get_health_checker()
        self.port = port
        self.app = web.Application()
        self._setup_routes()
        self._runner: Optional[web.AppRunner] = None
        self._site: Optional[web.TCPSite] = None
    
    def _setup_routes(self):
        """Setup HTTP routes."""
        self.app.router.add_get('/health', self.handle_health)
        self.app.router.add_get('/health/detailed', self.handle_detailed_health)
        self.app.router.add_get('/health/metrics', self.handle_metrics)
        self.app.router.add_get('/metrics', self.handle_metrics)
        self.app.router.add_get('/health/ready', self.handle_ready)
        self.app.router.add_get('/health/live', self.handle_live)
    
    async def handle_health(self, request: web.Request) -> Response:
        """Basic health check endpoint."""
        try:
            is_healthy = await self.health_checker.check_health()
            status_code = 200 if is_healthy else 503
            
            response = {
                "status": "healthy" if is_healthy else "unhealthy",
                "timestamp": datetime.utcnow().isoformat()
            }
            
            return web.json_response(response, status=status_code)
        except Exception as e:
            self.logger.error(f"Health check error: {e}", exc_info=True)
            return web.json_response(
                {"status": "error", "error": str(e)},
                status=500
            )
    
    async def handle_detailed_health(self, request: web.Request) -> Response:
        """Detailed health status endpoint."""
        try:
            health_status = await self.health_checker.get_detailed_health()
            
            # Add system info
            health_status["system"] = {
                "timestamp": datetime.utcnow().isoformat(),
                "uptime_seconds": self._get_uptime()
            }
            
            status_code = 200 if health_status.get("overall_healthy", False) else 503
            
            return web.json_response(health_status, status=status_code)
        except Exception as e:
            self.logger.error(f"Detailed health check error: {e}", exc_info=True)
            return web.json_response(
                {"status": "error", "error": str(e)},
                status=500
            )
    
    async def handle_metrics(self, request: web.Request) -> Response:
        """Prometheus metrics endpoint."""
        try:
            metrics_text = self.metrics_collector.export_prometheus()
            return Response(
                text=metrics_text,
                content_type='text/plain; version=0.0.4'
            )
        except Exception as e:
            self.logger.error(f"Metrics export error: {e}", exc_info=True)
            return Response(
                text=f"# Error exporting metrics: {e}\n",
                status=500,
                content_type='text/plain'
            )
    
    async def handle_ready(self, request: web.Request) -> Response:
        """Readiness probe endpoint."""
        try:
            is_ready = await self.health_checker.is_ready()
            status_code = 200 if is_ready else 503
            
            response = {
                "ready": is_ready,
                "timestamp": datetime.utcnow().isoformat()
            }
            
            return web.json_response(response, status=status_code)
        except Exception as e:
            self.logger.error(f"Readiness check error: {e}", exc_info=True)
            return web.json_response(
                {"ready": False, "error": str(e)},
                status=503
            )
    
    async def handle_live(self, request: web.Request) -> Response:
        """Liveness probe endpoint."""
        try:
            is_alive = await self.health_checker.is_alive()
            status_code = 200 if is_alive else 503
            
            response = {
                "alive": is_alive,
                "timestamp": datetime.utcnow().isoformat()
            }
            
            return web.json_response(response, status=status_code)
        except Exception as e:
            self.logger.error(f"Liveness check error: {e}", exc_info=True)
            return web.json_response(
                {"alive": False, "error": str(e)},
                status=503
            )
    
    def _get_uptime(self) -> float:
        """Get system uptime in seconds."""
        # This would track actual uptime
        # For now, return a placeholder
        import time
        if not hasattr(self, '_start_time'):
            self._start_time = time.time()
        return time.time() - self._start_time
    
    async def start(self):
        """Start the HTTP server."""
        self.logger.info(f"Starting health endpoint on port {self.port}")
        
        self._runner = web.AppRunner(self.app)
        await self._runner.setup()
        
        self._site = web.TCPSite(self._runner, '0.0.0.0', self.port)
        await self._site.start()
        
        self.logger.info(f"Health endpoint started on http://0.0.0.0:{self.port}")
    
    async def stop(self):
        """Stop the HTTP server."""
        if self._site:
            await self._site.stop()
        if self._runner:
            await self._runner.cleanup()
        self.logger.info("Health endpoint stopped")


# Global health endpoint instance
_health_endpoint: Optional[HealthEndpoint] = None


def get_health_endpoint() -> HealthEndpoint:
    """Get or create global health endpoint instance."""
    global _health_endpoint
    if _health_endpoint is None:
        _health_endpoint = HealthEndpoint()
    return _health_endpoint


async def start_health_endpoint(port: int = 8000) -> HealthEndpoint:
    """Start the health endpoint server."""
    endpoint = get_health_endpoint()
    endpoint.port = port
    await endpoint.start()
    return endpoint


__all__ = [
    'HealthEndpoint',
    'get_health_endpoint',
    'start_health_endpoint',
]
