"""
sessions.py - Enterprise session management module

Handles advanced session management, rotation, health tracking, and behavioral simulation
for military-grade anti-detection capabilities.
"""

import asyncio
import time
import random
import math
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any
from .interfaces import SessionProvider, ProxyProvider

logger = logging.getLogger(__name__)


class SessionHealth:
    """Represents session health metrics for monitoring"""
    
    def __init__(self):
        self.success_count = 0
        self.failure_count = 0
        self.last_success = None
        self.last_failure = None
        self.average_response_time = 0.0
        self.health_score = 1.0
        self.created_at = datetime.utcnow()
        self.last_updated = datetime.utcnow()
    
    def update_success(self, response_time: float) -> None:
        """Update metrics after successful request"""
        self.success_count += 1
        self.last_success = datetime.utcnow()
        
        # Update average response time
        total_requests = self.success_count + self.failure_count
        if total_requests > 0:
            self.average_response_time = (
                (self.average_response_time * (total_requests - 1) + response_time) / total_requests
            )
        
        self._calculate_health_score()
        self.last_updated = datetime.utcnow()
    
    def update_failure(self, error_type: str, response_time: float) -> None:
        """Update metrics after failed request"""
        self.failure_count += 1
        self.last_failure = datetime.utcnow()
        
        # Update average response time
        total_requests = self.success_count + self.failure_count
        if total_requests > 0:
            self.average_response_time = (
                (self.average_response_time * (total_requests - 1) + response_time) / total_requests
            )
        
        self._calculate_health_score()
        self.last_updated = datetime.utcnow()
    
    def _calculate_health_score(self) -> None:
        """Calculate overall health score based on metrics"""
        if self.failure_count == 0:
            # Perfect score if no failures
            self.health_score = 1.0
        else:
            # Score based on success rate
            success_rate = self.success_count / (self.success_count + self.failure_count)
            
            # Apply penalty for high failure rate
            if success_rate < 0.8:
                self.health_score = success_rate * 0.8  # Additional penalty
            elif success_rate < 0.9:
                self.health_score = success_rate * 0.9  # Minor penalty
            else:
                self.health_score = success_rate  # No penalty for good performance
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization"""
        return {
            "success_count": self.success_count,
            "failure_count": self.failure_count,
            "success_rate": self.success_count / max(1, self.success_count + self.failure_count),
            "average_response_time": self.average_response_time,
            "health_score": self.health_score,
            "last_success": self.last_success.isoformat() if self.last_success else None,
            "last_failure": self.last_failure.isoformat() if self.last_failure else None,
            "created_at": self.created_at.isoformat(),
            "last_updated": self.last_updated.isoformat()
        }


class SessionManager:
    """
    Enterprise-grade session management with rotation, health tracking, and anti-detection.
    
    Features:
    - Session pool management with health monitoring
    - Intelligent rotation based on performance and degradation
    - Military-grade fingerprint rotation
    - Behavioral simulation for human mimicry
    - Rate limiting and cooldown management
    """
    
    def __init__(self, config: Dict[str, Any], proxy_manager: Optional[ProxyProvider] = None):
        self.config = config
        self.proxy_manager = proxy_manager
        
        # Session configuration
        self.max_sessions = config.get("max_sessions", 10)
        self.session_timeout = config.get("session_timeout", 3600)  # 1 hour
        self.rotation_threshold = config.get("rotation_threshold", 0.3)  # Health score threshold
        self.cooldown_duration = config.get("cooldown_duration", 300)  # 5 minutes
        
        # Session pool
        self.sessions = {}
        self.session_health = {}
        self.session_fingerprints = {}
        self.session_cooldowns = {}
        
        # Anti-detection configuration
        self.anti_detection_enabled = config.get("anti_detection", True)
        self.fingerprint_rotation_enabled = config.get("fingerprint_rotation", True)
        self.behavioral_simulation_enabled = config.get("behavioral_simulation", True)
        
        # Initialize sessions
        self._initialize_sessions()
        
        logger.info(f"SessionManager initialized with {len(self.sessions)} sessions")
    
    def _initialize_sessions(self) -> None:
        """Initialize session pool with basic configuration"""
        for i in range(self.max_sessions):
            session_id = f"session_{i}"
            self.sessions[session_id] = {
                "id": session_id,
                "created_at": datetime.utcnow(),
                "last_used": None,
                "proxy_id": None,
                "health": SessionHealth(),
                "request_count": 0
            }
            self.session_health[session_id] = SessionHealth()
    
    async def get_healthy_session(self) -> str:
        """
        Get a healthy session for scraping operations.
        
        Returns:
            Session ID that is healthy and available
        """
        available_sessions = [
            session_id for session_id, session_data in self.sessions.items()
            if self._is_session_available(session_id, session_data)
        ]
        
        if not available_sessions:
            logger.warning("No healthy sessions available")
            raise Exception("No healthy sessions available")
        
        # Select best session based on health score and recent usage
        best_session = min(available_sessions, key=lambda x: (
            -self.session_health[x].health_score,
            self.sessions[x]["request_count"]
        ))
        
        # Mark session as used
        self.sessions[best_session]["last_used"] = datetime.utcnow()
        self.sessions[best_session]["request_count"] += 1
        
        logger.debug(f"Selected healthy session: {best_session}")
        return best_session
    
    def _is_session_available(self, session_id: str, session_data: Dict[str, Any]) -> bool:
        """Check if session is available for use"""
        # Check cooldown
        cooldown_until = self.session_cooldowns.get(session_id, 0)
        if time.time() < cooldown_until:
            return False
        
        # Check health
        health = self.session_health.get(session_id)
        if health and health.health_score < self.rotation_threshold:
            return False
        
        # Check timeout
        last_used = session_data.get("last_used")
        if last_used:
            session_age = time.time() - last_used.timestamp()
            if session_age > self.session_timeout:
                return False
        
        return True
    
    def rotate_session(self, session_id: str, reason: str) -> None:
        """
        Rotate a session due to health issues or scheduled rotation.
        
        Args:
            session_id: ID of session to rotate
            reason: Reason for rotation
        """
        logger.info(f"Rotating session {session_id} due to: {reason}")
        
        # Mark session as unhealthy
        if session_id in self.session_health:
            self.session_health[session_id].health_score = 0.0
        
        # Put session in cooldown
        self.session_cooldowns[session_id] = time.time() + self.cooldown_duration
        
        # Get new proxy if available
        if self.proxy_manager:
            try:
                new_proxy = self.proxy_manager.get_proxy_for_session(session_id)
                if new_proxy:
                    self.sessions[session_id]["proxy_id"] = new_proxy.get("proxy_id")
                    self.session_fingerprints[session_id] = self._generate_session_fingerprint(session_id, new_proxy)
            except Exception as e:
                logger.error(f"Failed to get new proxy for session {session_id}: {e}")
    
    def update_session_health(self, session_id: str, success: bool, response_time: float) -> None:
        """
        Update session health metrics after a request.
        
        Args:
            session_id: ID of the session
            success: Whether the request was successful
            response_time: Time taken for the request
        """
        if session_id in self.session_health:
            if success:
                self.session_health[session_id].update_success(response_time)
            else:
                self.session_health[session_id].update_failure("request_failed", response_time)
            
            # Check if session needs rotation
            if self.session_health[session_id].health_score < self.rotation_threshold:
                self.rotate_session(session_id, "health_threshold_breached")
    
    def _generate_session_fingerprint(self, session_id: str, proxy_config: Optional[Dict[str, Any]]) -> str:
        """
        Generate military-grade session fingerprint for anti-detection.
        
        Args:
            session_id: ID of the session
            proxy_config: Proxy configuration (optional)
            
        Returns:
            Fingerprint string for the session
        """
        if not self.fingerprint_rotation_enabled:
            return f"fp_{session_id[:8]}"
        
        # Device and browser options
        device_types = ["mobile", "tablet", "desktop", "smart_tv"]
        browsers = ["chrome", "safari", "firefox", "edge", "opera"]
        operating_systems = ["windows", "macos", "linux", "ios", "android"]
        
        # Select random components
        device = random.choice(device_types)
        browser = random.choice(browsers)
        os_type = random.choice(operating_systems)
        
        # Add entropy
        timestamp_entropy = str(int(time.time() * 1000))[-6:]
        uuid_entropy = f"{random.randint(1000, 9999):04d}"
        
        # Include proxy information if available
        proxy_info = ""
        if proxy_config:
            proxy_info = f"_proxy_{proxy_config.get('proxy_id', 'unknown')[:8]}"
        
        fingerprint = f"fp_{device}_{browser}_{os_type}_{timestamp_entropy}_{uuid_entropy}{proxy_info}"
        
        self.session_fingerprints[session_id] = fingerprint
        return fingerprint
    
    def get_session_headers(self, session_id: str) -> Dict[str, str]:
        """
        Get HTTP headers for a session with fingerprint rotation.
        
        Args:
            session_id: ID of the session
            
        Returns:
            Dictionary of HTTP headers
        """
        fingerprint = self.session_fingerprints.get(session_id, f"fp_{session_id[:8]}")
        
        # Base headers
        headers = {
            "User-Agent": f"TikTokScraper/2.0 ({fingerprint})",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache"
        }
        
        # Add anti-detection headers
        if self.anti_detection_enabled:
            headers.update({
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "same-origin",
                "Sec-Fetch-User": "?1",
                "DNT": "1",
                "Upgrade-Insecure-Requests": "1"
            })
        
        return headers
    
    def calculate_behavioral_jitter(self, session_id: str) -> float:
        """
        Calculate military-grade jitter with human-like unpredictability.
        
        Args:
            session_id: ID of the session
            
        Returns:
            Jitter delay in seconds
        """
        if not self.behavioral_simulation_enabled:
            return 0.0
        
        # Base jitter range
        min_jitter, max_jitter = 1.0, 5.0
        
        # Get session performance for adaptive jitter
        session_health = self.session_health.get(session_id)
        if session_health:
            # Adjust jitter based on session health
            health_factor = max(0.5, session_health.health_score)
            max_jitter *= health_factor
        
        # Golden ratio timing
        golden_ratio = (1 + math.sqrt(5)) / 2
        
        # Attention simulation (10% chance)
        if random.random() < 0.1:
            attention_time = random.uniform(2.0, 8.0)
            max_jitter += attention_time
        
        # Thinking time (5% chance)
        if random.random() < 0.05:
            thinking_time = random.uniform(1.0, 3.0)
            max_jitter += thinking_time
        
        # Distraction time (2% chance)
        if random.random() < 0.02:
            distraction_time = random.uniform(5.0, 15.0)
            max_jitter += distraction_time
        
        # Calculate final jitter
        jitter = random.uniform(min_jitter, max_jitter)
        
        # Apply golden ratio enhancement
        if random.random() < 0.3:
            jitter = jitter * golden_ratio
        elif random.random() < 0.7:
            jitter = jitter * (golden_ratio * 1.5)
        
        logger.debug(f"Calculated jitter for session {session_id}: {jitter:.2f}s")
        return jitter
    
    def get_session_stats(self) -> Dict[str, Any]:
        """
        Get comprehensive session statistics for monitoring.
        
        Returns:
            Dictionary with session statistics
        """
        stats = {
            "total_sessions": len(self.sessions),
            "healthy_sessions": len([
                sid for sid, health in self.session_health.items()
                if health.health_score >= self.rotation_threshold
            ]),
            "sessions_in_cooldown": len(self.session_cooldowns),
            "average_health_score": sum(
                health.health_score for health in self.session_health.values()
            ) / len(self.session_health) if self.session_health else 0,
            "total_requests": sum(
                session_data["request_count"] for session_data in self.sessions.values()
            ),
            "session_details": {}
        }
        
        # Add detailed session information
        for session_id, session_data in self.sessions.items():
            health = self.session_health.get(session_id)
            stats["session_details"][session_id] = {
                "created_at": session_data["created_at"].isoformat(),
                "last_used": session_data["last_used"].isoformat() if session_data["last_used"] else None,
                "request_count": session_data["request_count"],
                "health_score": health.health_score if health else 0.0,
                "health_details": health.to_dict() if health else None,
                "fingerprint": self.session_fingerprints.get(session_id),
                "proxy_id": session_data.get("proxy_id"),
                "in_cooldown": session_id in self.session_cooldowns
            }
        
        return stats
    
    async def cleanup(self) -> None:
        """Cleanup session resources"""
        logger.info("SessionManager cleanup initiated")
        
        # Clear all session data
        self.sessions.clear()
        self.session_health.clear()
        self.session_fingerprints.clear()
        self.session_cooldowns.clear()
        
        logger.info("SessionManager cleanup completed")
