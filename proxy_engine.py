""" Jubra Traffic Pro - Multi-Protocol Proxy Engine Complete proxy lifecycle management with health scoring, intelligent rotation, ban detection, and geo-awareness. """

import asyncio
import time
import uuid
import json
import random
import hashlib
import logging
import ipaddress
import aiohttp
import aiofiles
try:
    from aiohttp_socks import ProxyConnector
    HAS_AIOHTTP_SOCKS = True
except ImportError:
    ProxyConnector = None
    HAS_AIOHTTP_SOCKS = False
from enum import Enum, auto
from dataclasses import dataclass, field
from typing import (
    Any, Dict, List, Optional, Set, Tuple, AsyncIterator, Callable, Union
)
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from urllib.parse import urlparse

from core.exceptions import (
    ProxyError,
    ProxyConnectionError,
    ProxyAuthenticationError,
    ProxyPoolExhaustedError,
    ProxyBannedError,
    ErrorContext,
)
from core.event_bus import (
    EventBus,
    EventCategory,
    EventPriority,
    get_event_bus,
)
from core.config_manager import ConfigManager

logger = logging.getLogger(__name__)

# ==============================================================================
# Proxy Protocol & Type
# ==============================================================================

class ProxyProtocol(Enum):
    HTTP = "http"
    HTTPS = "https"
    SOCKS4 = "socks4"
    SOCKS5 = "socks5"

class ProxyType(Enum):
    DATACENTER = "datacenter"
    RESIDENTIAL = "residential"
    MOBILE = "mobile"
    ISP = "isp"
    TOR = "tor"

class ProxyStatus(Enum):
    UNKNOWN = "unknown"
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    FAILED = "failed"
    BANNED = "banned"
    COOLDOWN = "cooldown"
    IN_USE = "in_use"
    RESERVED = "reserved"

class RotationStrategy(Enum):
    ROUND_ROBIN = "round_robin"
    WEIGHTED = "weighted"
    RANDOM = "random"
    STICKY = "sticky"
    LEAST_USED = "least_used"
    LEAST_FAILED = "least_failed"
    GEO_OPTIMAL = "geo_optimal"
    PERFORMANCE = "performance"

# ==============================================================================
# Proxy Health Score
# ==============================================================================

@dataclass
class ProxyHealthScore:
    """
    Multi-dimensional health score for a proxy.
    Updated in real-time based on check results and usage.
    """
    # Core score 0.0-1.0 overall
    overall: float = 1.0

    # Sub-scores
    availability:       float = 1.0    # % successful connections
    speed:              float = 1.0    # inverse latency score
    anonymity:          float = 1.0    # how well it hides origin
    reliability:        float = 1.0    # consistency over time
    freshness:          float = 1.0    # recency of validation
    
    # Raw stats
    total_requests:     int   = 0
    successful:         int   = 0
    failed:             int   = 0
    timeouts:           int   = 0
    bans:               int   = 0
    captchas:           int   = 0
    
    # Latency tracking (ms)
    latencies:          deque = field(default_factory=lambda: deque(maxlen=50))
    last_check_time:    float = 0.0
    last_success_time:  float = 0.0
    last_failure_time:  float = 0.0

    def update(
        self,
        success:    bool,
        latency_ms: float = 0.0,
        banned:     bool  = False,
        captcha:    bool  = False,
    ) -> None:
        """Update health score after a request attempt."""
        self.total_requests += 1
        now = time.monotonic()
        if success:
            self.successful += 1
            self.last_success_time = now
            if latency_ms > 0:
                self.latencies.append(latency_ms)
        else:
            self.failed += 1
            self.last_failure_time = now
            if banned:
                self.bans += 1
            if captcha:
                self.captchas += 1
        self._recalculate()

    def _recalculate(self) -> None:
        """Recompute all sub-scores and overall score."""
        # Availability score
        if self.total_requests > 0:
            self.availability = self.successful / self.total_requests
        else:
            self.availability = 1.0
            
        # Speed score (based on median latency)
        if self.latencies:
            median_ms = sorted(self.latencies)[len(self.latencies) // 2]
            # Map: <200ms=1.0, 500ms=0.7, 1000ms=0.4, >2000ms=0.1
            self.speed = max(0.1, min(1.0, 1.0 - (median_ms - 200) / 2000))
        else:
            self.speed = 0.5
            
        # Reliability: exponential decay of failure impact
        recent_window = min(self.total_requests, 20)
        recent_failed = min(self.failed, recent_window // 2)
        self.reliability = max(0.0, 1.0 - (recent_failed / max(recent_window, 1)))
        
        # Freshness: time since last check
        if self.last_check_time > 0:
            age_minutes = (time.monotonic() - self.last_check_time) / 60
            self.freshness = max(0.0, 1.0 - (age_minutes / 60))
        else:
            self.freshness = 0.5
            
        # Ban penalty
        ban_penalty = min(1.0, self.bans * 0.25)
        
        # Overall weighted score
        self.overall = (
            self.availability * 0.35 +
            self.speed        * 0.20 +
            self.reliability  * 0.25 +
            self.freshness    * 0.10 +
            self.anonymity    * 0.10
        ) * (1.0 - ban_penalty)
        self.overall = max(0.0, min(1.0, self.overall))

    @property
    def avg_latency_ms(self) -> float:
        if not self.latencies:
            return 0.0
        return sum(self.latencies) / len(self.latencies)

    @property
    def p95_latency_ms(self) -> float:
        if not self.latencies:
            return 0.0
        sorted_l = sorted(self.latencies)
        idx = int(len(sorted_l) * 0.95)
        return sorted_l[min(idx, len(sorted_l) - 1)]

    @property
    def success_rate(self) -> float:
        if self.total_requests == 0:
            return 1.0
        return self.successful / self.total_requests

    @property
    def is_healthy(self) -> bool:
        return self.overall >= 0.4 and self.availability >= 0.3

    def to_dict(self) -> Dict[str, Any]:
        return {
            "overall":          round(self.overall, 4),
            "availability":     round(self.availability, 4),
            "speed":            round(self.speed, 4),
            "reliability":      round(self.reliability, 4),
            "freshness":        round(self.freshness, 4),
            "anonymity":        round(self.anonymity, 4),
            "total_requests":   self.total_requests,
            "successful":       self.successful,
            "failed":           self.failed,
            "timeouts":         self.timeouts,
            "bans":             self.bans,
            "captchas":         self.captchas,
            "avg_latency_ms":   round(self.avg_latency_ms, 2),
            "p95_latency_ms":   round(self.p95_latency_ms, 2),
            "success_rate":     round(self.success_rate, 4),
        }

# ==============================================================================
# Proxy Geo Information
# ==============================================================================

@dataclass
class ProxyGeoInfo:
    """Geographic and network information for a proxy."""
    ip: str = ""
    country_code: str = ""
    country_name: str = ""
    region: str = ""
    city: str = ""
    postal_code: str = ""
    latitude: float = 0.0
    longitude: float = 0.0
    timezone: str = ""
    isp: str = ""
    org: str = ""
    asn: str = ""
    is_datacenter: bool = False
    is_vpn: bool = False
    is_tor: bool = False
    is_proxy: bool = True
    threat_score: float = 0.0  # 0.0=clean, 1.0=high threat
    resolved_at: float = 0.0

    @classmethod
    def from_api_response(cls, data: Dict[str, Any]) -> "ProxyGeoInfo":
        """Build from ipapi/ipinfo/maxmind API response."""
        return cls(
            ip           = data.get("ip", ""),
            country_code = data.get("country_code", data.get("country", "")),
            country_name = data.get("country_name", ""),
            region       = data.get("region", data.get("regionName", "")),
            city         = data.get("city", ""),
            postal_code  = data.get("zip", data.get("postal", "")),
            latitude     = float(data.get("lat", data.get("latitude", 0))),
            longitude    = float(data.get("lon", data.get("longitude", 0))),
            timezone     = data.get("timezone", ""),
            isp          = data.get("isp", data.get("org", "")),
            org          = data.get("org", ""),
            asn          = str(data.get("as", data.get("asn", ""))),
            is_datacenter = data.get("hosting", False),
            is_vpn       = data.get("vpn", False),
            is_tor       = data.get("tor", False),
            is_proxy     = data.get("proxy", True),
            threat_score = float(data.get("threat_score", 0.0)),
            resolved_at  = time.monotonic(),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ip":           self.ip,
            "country":      self.country_code,
            "city":         self.city,
            "region":       self.region,
            "isp":          self.isp,
            "asn":          self.asn,
            "timezone":     self.timezone,
            "datacenter":   self.is_datacenter,
            "threat_score": round(self.threat_score, 3),
        }

# ==============================================================================
# Proxy Object
# ==============================================================================

class Proxy:
    """
    Complete proxy object with full lifecycle management.
    """
    def __init__(
        self,
        host:       str,
        port:       int,
        protocol:   ProxyProtocol           = ProxyProtocol.HTTP,
        username:   Optional[str]           = None,
        password:   Optional[str]           = None,
        proxy_type: ProxyType               = ProxyType.DATACENTER,
        country:    str                     = "",
        tags:       Optional[Set[str]]      = None,
        weight:     float                   = 1.0,
        proxy_id:   Optional[str]           = None,
        max_concurrent: int                 = 5,
    ):
        self.proxy_id       = proxy_id or str(uuid.uuid4())[:12]
        self.host           = host
        self.port           = port
        self.protocol       = protocol
        self.username       = username
        self.password       = password
        self.proxy_type     = proxy_type
        self.country        = country.upper()
        self.tags           = tags or set()
        self.weight         = weight
        self.max_concurrent = max_concurrent
        
        # State
        self._status        = ProxyStatus.UNKNOWN
        self._status_lock   = asyncio.Lock()
        
        # Health
        self.health         = ProxyHealthScore()
        
        # Geo info
        self.geo            = ProxyGeoInfo()
        
        # Usage tracking
        self._active_sessions:   Set[str]           = set()
        self._domain_stats:      Dict[str, Dict]    = defaultdict(lambda: {
            "requests": 0, "successes": 0, "failures": 0,
            "bans": 0, "last_used": 0.0,
        })
        self._banned_domains:    Dict[str, float]   = {}  # domain -> unban_time
        
        # Cooldown
        self._cooldown_until:    float = 0.0
        self._ban_until:         float = 0.0
        
        # Sticky session binding
        self._bound_sessions:    Dict[str, str]     = {}  # session_id -> proxy_id (self)
        
        # Timing
        self.created_at     = time.monotonic()
        self.last_used_at:  float = 0.0
        self.last_check_at: float = 0.0
        logger.debug(
            f"[Proxy] Created: {self.proxy_id} | "
            f"{self.url_masked} | type={proxy_type.value}"
        )

    @property
    def url(self) -> str:
        """Full proxy URL including credentials."""
        auth = ""
        if self.username and self.password:
            from urllib.parse import quote
            auth = f"{quote(self.username, safe='')}:{quote(self.password, safe='')}@"
        return f"{self.protocol.value}://{auth}{self.host}:{self.port}"

    @property
    def url_masked(self) -> str:
        """Proxy URL with masked credentials for logging."""
        auth = f"***:***@" if self.username else ""
        return f"{self.protocol.value}://{auth}{self.host}:{self.port}"

    @property
    def address(self) -> str:
        return f"{self.host}:{self.port}"

    @property
    def connection_dict(self) -> Dict[str, Any]:
        """Dict format for aiohttp/requests."""
        return {
            "http":  self.url,
            "https": self.url,
        }

    @property
    def status(self) -> ProxyStatus:
        return self._status

    async def set_status(self, status: ProxyStatus) -> None:
        async with self._status_lock:
            old = self._status
            self._status = status
            if old != status:
                logger.debug(
                    f"[Proxy] {self.proxy_id}: "
                    f"{old.value} → {status.value}"
                )

    @property
    def is_available(self) -> bool:
        """Check if proxy can accept new sessions."""
        if self._status in {ProxyStatus.BANNED, ProxyStatus.FAILED}:
            return False
        if self.is_in_cooldown:
            return False
        if len(self._active_sessions) >= self.max_concurrent:
            return False
        return True

    @property
    def is_in_cooldown(self) -> bool:
        return time.monotonic() < self._cooldown_until

    @property
    def is_banned(self) -> bool:
        if time.monotonic() < self._ban_until:
            return True
        if self._status == ProxyStatus.BANNED:
            if time.monotonic() >= self._ban_until:
                asyncio.create_task(self.set_status(ProxyStatus.UNKNOWN))
                return False
        return False

    @property
    def cooldown_remaining(self) -> float:
        return max(0.0, self._cooldown_until - time.monotonic())

    @property
    def concurrent_count(self) -> int:
        return len(self._active_sessions)

    def acquire(self, session_id: str) -> bool:
        """Mark proxy as acquired by a session."""
        if not self.is_available:
            return False
        self._active_sessions.add(session_id)
        self.last_used_at = time.monotonic()
        return True

    def release(self, session_id: str) -> None:
        """Release proxy from a session."""
        self._active_sessions.discard(session_id)

    def is_acquired_by(self, session_id: str) -> bool:
        return session_id in self._active_sessions

    async def ban(
        self,
        duration_seconds: float     = 3600.0,
        domain:           str       = "",
    ) -> None:
        """Ban this proxy (globally or for a specific domain)."""
        if domain:
            self._banned_domains[domain] = time.monotonic() + duration_seconds
            self._domain_stats[domain]["bans"] += 1
            logger.warning(
                f"[Proxy] {self.proxy_id} banned for domain "
                f"{domain!r} for {duration_seconds:.0f}s"
            )
        else:
            self._ban_until = time.monotonic() + duration_seconds
            await self.set_status(ProxyStatus.BANNED)
            logger.warning(
                f"[Proxy] {self.proxy_id} globally banned "
                f"for {duration_seconds:.0f}s"
            )

    async def cooldown(self, duration_seconds: float = 30.0) -> None:
        """Put proxy in cooldown (temporary unavailability)."""
        self._cooldown_until = time.monotonic() + duration_seconds
        await self.set_status(ProxyStatus.COOLDOWN)

    def is_banned_for_domain(self, domain: str) -> bool:
        ban_time = self._banned_domains.get(domain)
        if ban_time is None:
            return False
        if time.monotonic() > ban_time:
            del self._banned_domains[domain]
            return False
        return True

    def record_success(
        self,
        latency_ms: float = 0.0,
        domain:     str   = "",
    ) -> None:
        """Record a successful request through this proxy."""
        self.health.update(success=True, latency_ms=latency_ms)
        self.health.last_check_time = time.monotonic()
        if domain:
            stats = self._domain_stats[domain]
            stats["requests"] += 1
            stats["successes"] += 1
            stats["last_used"] = time.monotonic()

    def record_failure(
        self,
        domain:   str   = "",
        banned:   bool  = False,
        captcha:  bool  = False,
        timeout:  bool  = False,
    ) -> None:
        """Record a failed request through this proxy."""
        self.health.update(success=False, banned=banned, captcha=captcha)
        if timeout:
            self.health.timeouts += 1
        if domain:
            stats = self._domain_stats[domain]
            stats["requests"] += 1
            stats["failures"] += 1
            stats["last_used"] = time.monotonic()

    def update_geo(self, geo: ProxyGeoInfo) -> None:
        """Update geographic information."""
        self.geo = geo
        if geo.country_code:
            self.country = geo.country_code

    def to_dict(self) -> Dict[str, Any]:
        return {
            "proxy_id":         self.proxy_id,
            "address":          self.address,
            "protocol":         self.protocol.value,
            "proxy_type":       self.proxy_type.value,
            "country":          self.country,
            "status":           self._status.value,
            "is_available":     self.is_available,
            "concurrent":       self.concurrent_count,
            "max_concurrent":   self.max_concurrent,
            "weight":           self.weight,
            "tags":             list(self.tags),
            "health":           self.health.to_dict(),
            "geo":              self.geo.to_dict(),
            "last_used":        self.last_used_at,
            "last_check":       self.last_check_at,
            "banned_domains":   list(self._banned_domains.keys()),
            "cooldown_remaining": round(self.cooldown_remaining, 1),
        }

    def to_connection_string(self) -> str:
        """Export as connection string for file storage."""
        auth = ""
        if self.username and self.password:
            auth = f"{self.username}:{self.password}@"
        return f"{self.protocol.value}://{auth}{self.host}:{self.port}"

    @classmethod
    def from_string(
        cls,
        proxy_str: str,
        default_protocol: Union[str, ProxyProtocol] = ProxyProtocol.HTTP,
    ) -> "Proxy":
        """
        Parse proxy from common text formats safely:
        - host:port
        - host:port:user:pass
        - host:port:user:pass:with:colon
        - user:pass@host:port
        - http://user:pass@host:port
        - socks5://host:port:user:pass
        """
        proxy_str = proxy_str.strip()
        if not proxy_str:
            raise ValueError("Empty proxy string")

        if isinstance(default_protocol, ProxyProtocol):
            protocol = default_protocol
        else:
            try:
                protocol = ProxyProtocol(str(default_protocol).strip().lower())
            except Exception:
                protocol = ProxyProtocol.HTTP

        if "://" in proxy_str:
            scheme_part, proxy_str = proxy_str.split("://", 1)
            try:
                protocol = ProxyProtocol(scheme_part.lower())
            except Exception:
                protocol = ProxyProtocol.HTTP

        username = password = None

        if "@" in proxy_str:
            auth_part, host_port = proxy_str.rsplit("@", 1)
            if ":" in auth_part:
                username, password = auth_part.split(":", 1)
            else:
                username = auth_part
            hp_parts = host_port.rsplit(":", 1)
            if len(hp_parts) != 2:
                raise ValueError(f"Invalid proxy host/port: {proxy_str}")
            host, port_str = hp_parts[0], hp_parts[1]
        else:
            parts = proxy_str.split(":")
            if len(parts) < 2:
                raise ValueError(f"Invalid proxy format: {proxy_str}")
            host, port_str = parts[0], parts[1]
            if len(parts) >= 4:
                username = parts[2]
                password = ":".join(parts[3:])
            elif len(parts) == 3:
                raise ValueError(
                    f"Invalid proxy auth format, expected host:port:user:pass: {proxy_str}"
                )

        host = (host or "").strip()
        if not host:
            raise ValueError(f"Invalid proxy host: {proxy_str}")
        try:
            port = int(str(port_str).strip())
        except Exception as exc:
            raise ValueError(f"Invalid proxy port: {proxy_str}") from exc
        if not (1 <= port <= 65535):
            raise ValueError(f"Proxy port out of range: {proxy_str}")

        return cls(
            host=host,
            port=port,
            protocol=protocol,
            username=username,
            password=password,
        )

    def __repr__(self) -> str:
        return (
            f"Proxy("
            f"id={self.proxy_id}, "
            f"{self.protocol.value}://{self.host}:{self.port}, "
            f"status={self._status.value}, "
            f"score={self.health.overall:.3f})"
        )

    def __hash__(self) -> int:
        return hash(self.proxy_id)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Proxy):
            return False
        return self.proxy_id == other.proxy_id

# ==============================================================================
# Proxy Pool Statistics
# ==============================================================================

class ProxyPoolStats:
    """Aggregated statistics for the entire proxy pool."""
    def __init__(self):
        self.total_requests:    int   = 0
        self.total_successes:   int   = 0
        self.total_failures:    int   = 0
        self.total_bans:        int   = 0
        self.total_rotations:   int   = 0
        self.total_captchas:    int   = 0
        self._rotation_times:   deque = deque(maxlen=1000)
        self._start_time:       float = time.monotonic()

    def record_rotation(self) -> None:
        self.total_rotations += 1
        self._rotation_times.append(time.monotonic())

    def record_request(self, success: bool, banned: bool = False) -> None:
        self.total_requests += 1
        if success:
            self.total_successes += 1
        else:
            self.total_failures += 1
        if banned:
            self.total_bans += 1

    @property
    def success_rate(self) -> float:
        if self.total_requests == 0:
            return 1.0
        return self.total_successes / self.total_requests

    @property
    def rotations_per_hour(self) -> float:
        now = time.monotonic()
        recent = [t for t in self._rotation_times if now - t <= 3600]
        return len(recent)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total_requests":   self.total_requests,
            "total_successes":  self.total_successes,
            "total_failures":   self.total_failures,
            "total_bans":       self.total_bans,
            "total_rotations":  self.total_rotations,
            "success_rate":     round(self.success_rate, 4),
            "rotations_per_hr": round(self.rotations_per_hour, 1),
        }

# ==============================================================================
# Proxy Engine
# ==============================================================================

class ProxyEngine:
    """
    Jubra Traffic Pro - Multi-Protocol Proxy Engine with Robust Parsing and Validation
    """
    GEO_CHECK_URLS = [
        "http://ip-api.com/json/?fields=status,country,countryCode,"
        "region,regionName,city,zip,lat,lon,timezone,isp,org,as,"
        "hosting,proxy,query",
        "https://ipapi.co/json/",
    ]
    CONNECTIVITY_CHECK_URLS = [
        "https://httpbin.org/ip",
        "https://api.ipify.org?format=json",
        "https://ifconfig.me/ip",
    ]

    def __init__(
        self,
        config:                 ConfigManager,
        event_bus:              Optional[EventBus] = None,
        rotation_strategy:      RotationStrategy   = RotationStrategy.WEIGHTED,
        health_check_interval:  float              = 60.0,
        health_check_timeout:   float              = 10.0,
        health_check_url:       str                = "https://httpbin.org/ip",
        max_failures:           int                = 3,
        ban_duration:           float              = 3600.0,
        low_pool_threshold:     int                = 5,
        min_health_score:       float              = 0.35,
        max_concurrent_checks:  int                = 20,
    ):
        self._config                = config
        self._event_bus             = event_bus or get_event_bus()
        self._rotation_strategy     = rotation_strategy
        self._health_check_interval = health_check_interval
        self._health_check_timeout  = health_check_timeout
        self._health_check_url      = config.get("proxy.health_check_url", health_check_url)
        self._max_failures          = max_failures
        self._ban_duration          = ban_duration
        self._low_pool_threshold    = low_pool_threshold
        self._min_health_score      = min_health_score
        self._max_concurrent_checks = max_concurrent_checks
        
        # Proxy storage
        self._proxies:          Dict[str, Proxy]    = {}
        self._proxy_list:       List[Proxy]         = []
        self._round_robin_idx:  int                 = 0
        self._sticky_map:       Dict[str, str]      = {}  # session_id -> proxy_id
        self._lock              = asyncio.Lock()
        
        # Statistics
        self.stats              = ProxyPoolStats()
        
        # Background tasks
        self._health_task:      Optional[asyncio.Task] = None
        self._running:          bool = False
        
        # Semaphore for concurrent health checks
        self._check_semaphore   = asyncio.Semaphore(max_concurrent_checks)
        logger.info(
            f"[ProxyEngine] Initialized: "
            f"strategy={rotation_strategy.value}, "
            f"health_interval={health_check_interval}s"
        )

    async def start(self) -> None:
        """Start proxy engine and background health checker."""
        self._running = True
        self._health_task = asyncio.create_task(
            self._health_check_loop(),
            name="ProxyEngine-HealthChecker",
        )
        logger.info(f"[ProxyEngine] Started with {len(self._proxies)} proxies")

    async def stop(self) -> None:
        """Stop proxy engine."""
        self._running = False
        if self._health_task:
            self._health_task.cancel()
            try:
                await self._health_task
            except asyncio.CancelledError:
                pass
        logger.info("[ProxyEngine] Stopped")

    async def load_from_file(
        self,
        filepath:       str,
        validate:       bool = False,
        proxy_type:     ProxyType = ProxyType.DATACENTER,
    ) -> int:
        """
        Load proxies from file. Lines with errors are safely skipped.
        """
        path_obj = __import__("pathlib").Path(filepath)
        if not path_obj.exists():
            logger.warning(f"[ProxyEngine] Proxy file not found: {filepath}")
            return 0
        loaded = 0
        try:
            async with aiofiles.open(filepath, "r") as f:
                content = await f.read()

            # A file reload should reflect the current file contents. Replace the
            # old pool first so stale failed proxies do not remain active after
            # Save & Apply in the GUI.
            async with self._lock:
                self._proxies.clear()
                self._proxy_list.clear()
                self._round_robin_idx = 0
                self._sticky_map.clear()

            default_protocol = str(
                self._config.get("proxy.default_protocol", "http") or "http"
            ).strip().lower()

            # Try JSON array first
            if content.strip().startswith("["):
                proxy_data_list = json.loads(content)
                for item in proxy_data_list:
                    proxy = self._parse_proxy_dict(item, proxy_type)
                    if proxy:
                        await self._add_proxy(proxy)
                        loaded += 1
            else:
                # Text format line-by-line
                for line in content.splitlines():
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    try:
                        proxy = Proxy.from_string(
                            line,
                            default_protocol=default_protocol,
                        )
                        proxy.proxy_type = proxy_type
                        await self._add_proxy(proxy)
                        loaded += 1
                    except Exception as exc:
                        logger.warning(f"[ProxyEngine] Skip invalid proxy line: {line!r} | Error: {exc}")
                        continue
        except Exception as exc:
            logger.error(f"[ProxyEngine] Load failed from {filepath}: {exc}")
            return 0

        logger.info(f"[ProxyEngine] Successfully loaded {loaded} proxies from {filepath}")
        
        if validate and loaded > 0:
            validated = await self.validate_all(concurrent=min(loaded, 50))
            logger.info(
                f"[ProxyEngine] Validated: {validated}/{loaded} healthy"
            )
        await self._emit_pool_event()
        return loaded

    async def load_from_list(
        self,
        proxy_strings:  List[str],
        proxy_type:     ProxyType = ProxyType.DATACENTER,
        validate:       bool      = False,
    ) -> int:
        """Load proxies from a list of strings."""
        loaded = 0
        for proxy_str in proxy_strings:
            try:
                proxy = Proxy.from_string(proxy_str)
                proxy.proxy_type = proxy_type
                await self._add_proxy(proxy)
                loaded += 1
            except Exception as exc:
                logger.debug(f"[ProxyEngine] Skip: {proxy_str!r}: {exc}")
        if validate:
            await self.validate_all(concurrent=min(loaded, 50))
        return loaded

    async def add_proxy(self, proxy: Proxy) -> None:
        """Add a single proxy to the pool."""
        await self._add_proxy(proxy)

    async def remove_proxy(self, proxy_id: str) -> bool:
        """Remove a proxy from the pool by ID."""
        async with self._lock:
            proxy = self._proxies.pop(proxy_id, None)
            if proxy:
                self._proxy_list = [p for p in self._proxy_list if p.proxy_id != proxy_id]
                logger.debug(f"[ProxyEngine] Removed proxy: {proxy_id}")
                return True
            return False

    async def acquire_proxy(
        self,
        session_id:     str,
        country:        Optional[str]       = None,
        proxy_type:     Optional[ProxyType] = None,
        domain:         Optional[str]       = None,
        exclude_ids:    Optional[Set[str]]  = None,
        min_score:      Optional[float]     = None,
    ) -> Proxy:
        """
        Acquire a proxy for a session using the configured rotation strategy.
        """
        async with self._lock:
            # Check sticky session
            if self._rotation_strategy == RotationStrategy.STICKY:
                if session_id in self._sticky_map:
                    proxy_id = self._sticky_map[session_id]
                    proxy = self._proxies.get(proxy_id)
                    if proxy and proxy.is_available:
                        proxy.acquire(session_id)
                        self.stats.record_rotation()
                        return proxy
                        
            # Filter candidates
            candidates = self._filter_candidates(
                country=country,
                proxy_type=proxy_type,
                domain=domain,
                exclude_ids=exclude_ids or set(),
                min_score=min_score or self._min_health_score,
            )
            if not candidates:
                # Try with relaxed constraints
                candidates = self._filter_candidates(
                    country=None,
                    proxy_type=proxy_type,
                    domain=domain,
                    exclude_ids=exclude_ids or set(),
                    min_score=0.1,
                )
            if not candidates:
                available_total = sum(
                    1 for p in self._proxies.values() if p.is_available
                )
                raise ProxyPoolExhaustedError(
                    total_proxies=len(self._proxies),
                    context=ErrorContext(
                        module="ProxyEngine",
                        operation="acquire_proxy",
                        session_id=session_id,
                    ),
                )
                
            # Select using strategy
            selected = self._select_proxy(candidates, session_id)
            if not selected.acquire(session_id):
                raise ProxyPoolExhaustedError(
                    total_proxies=len(self._proxies),
                )
                
            # Update sticky map
            if self._rotation_strategy == RotationStrategy.STICKY:
                self._sticky_map[session_id] = selected.proxy_id
            self.stats.record_rotation()
            await self._event_bus.publish_simple(
                EventCategory.PROXY_ACQUIRED,
                {
                    "proxy_id":   selected.proxy_id,
                    "address":    selected.address,
                    "country":    selected.country,
                    "session_id": session_id,
                    "score":      round(selected.health.overall, 3),
                    "pool_size":  len(candidates),
                },
                priority=EventPriority.LOW,
                session_id=session_id,
            )
            logger.debug(
                f"[ProxyEngine] Acquired: {selected.proxy_id} "
                f"({selected.address}) for session {session_id[:8]}"
            )
            
            # Check low pool warning
            available = sum(1 for p in self._proxies.values() if p.is_available)
            if available <= self._low_pool_threshold:
                await self._event_bus.publish_simple(
                    EventCategory.PROXY_POOL_LOW,
                    {
                        "available": available,
                        "total":     len(self._proxies),
                        "threshold": self._low_pool_threshold,
                    },
                    priority=EventPriority.HIGH,
                )
            return selected

    async def release_proxy(
        self,
        proxy_id:   str,
        session_id: str,
        success:    bool  = True,
        latency_ms: float = 0.0,
        domain:     str   = "",
        banned:     bool  = False,
        captcha:    bool  = False,
    ) -> None:
        """Release a proxy back to the pool and update health."""
        proxy = self._proxies.get(proxy_id)
        if not proxy:
            return
        proxy.release(session_id)
        self.stats.record_request(success=success, banned=banned)
        if success:
            proxy.record_success(latency_ms=latency_ms, domain=domain)
        else:
            proxy.record_failure(
                domain=domain,
                banned=banned,
                captcha=captcha,
            )
        # Handle ban
        if banned:
            await self._handle_ban(proxy, domain)
        # Update status based on health
        await self._update_proxy_status(proxy)
        await self._event_bus.publish_simple(
            EventCategory.PROXY_RELEASED,
            {
                "proxy_id":   proxy_id,
                "session_id": session_id,
                "success":    success,
                "latency_ms": round(latency_ms, 2),
                "banned":     banned,
                "score":      round(proxy.health.overall, 3),
            },
            priority=EventPriority.LOW,
        )

    @asynccontextmanager
    async def proxy_context(
        self,
        session_id: str,
        **acquire_kwargs,
    ) -> AsyncIterator[Proxy]:
        proxy = await self.acquire_proxy(session_id, **acquire_kwargs)
        success = True
        latency_start = time.monotonic()
        try:
            yield proxy
        except Exception:
            success = False
            raise
        finally:
            latency_ms = (time.monotonic() - latency_start) * 1000
            await self.release_proxy(
                proxy_id=proxy.proxy_id,
                session_id=session_id,
                success=success,
                latency_ms=latency_ms,
            )

    async def validate_proxy(
        self,
        proxy:      Proxy,
        check_geo:  bool = True,
    ) -> bool:
        """Validate proxy connectivity with fallback URLs and clear diagnostics."""
        async with self._check_semaphore:
            last_error = "unknown validation error"
            urls = []
            configured_urls = self._config.get("proxy.validation_urls", [])
            if isinstance(configured_urls, list):
                urls.extend(str(u).strip() for u in configured_urls if str(u).strip())
            urls.append(self._health_check_url)
            urls.extend(self.CONNECTIVITY_CHECK_URLS)

            # Preserve order while removing duplicates.
            seen = set()
            check_urls = []
            for url in urls:
                if url and url not in seen:
                    seen.add(url)
                    check_urls.append(url)

            try:
                is_socks = proxy.protocol in {
                    ProxyProtocol.SOCKS4,
                    ProxyProtocol.SOCKS5,
                }
                if is_socks:
                    if not HAS_AIOHTTP_SOCKS:
                        last_error = "aiohttp_socks missing; run: pip install aiohttp_socks"
                        raise RuntimeError(last_error)
                    connector = ProxyConnector.from_url(proxy.url)
                    proxy_arg = None
                else:
                    connector = aiohttp.TCPConnector(ssl=False)
                    proxy_arg = proxy.url
                timeout = aiohttp.ClientTimeout(total=self._health_check_timeout)
                async with aiohttp.ClientSession(
                    connector=connector,
                    timeout=timeout,
                ) as session:
                    for check_url in check_urls:
                        start = time.monotonic()
                        try:
                            async with session.get(
                                check_url,
                                proxy=proxy_arg,
                                allow_redirects=True,
                            ) as resp:
                                latency_ms = (time.monotonic() - start) * 1000
                                if 200 <= resp.status < 300:
                                    proxy.record_success(latency_ms=latency_ms)
                                    proxy.last_check_at = time.monotonic()
                                    if check_geo and not proxy.geo.country_code:
                                        await self._fetch_geo(
                                            proxy,
                                            session,
                                            proxy_arg=proxy_arg,
                                        )
                                    await proxy.set_status(ProxyStatus.HEALTHY)
                                    logger.info(
                                        f"[ProxyEngine] Proxy healthy: "
                                        f"{proxy.url_masked} ({latency_ms:.0f}ms)"
                                    )
                                    return True
                                last_error = f"HTTP {resp.status} from {check_url}"
                        except asyncio.TimeoutError:
                            last_error = f"timeout at {check_url}"
                        except aiohttp.ClientHttpProxyError as exc:
                            last_error = f"proxy HTTP/auth error: {exc.status} {exc.message}"
                            break
                        except aiohttp.ClientProxyConnectionError as exc:
                            last_error = f"proxy connection error: {exc}"
                            break
                        except Exception as exc:
                            last_error = f"{type(exc).__name__}: {exc}"
                            continue
            except Exception as exc:
                last_error = f"session error: {type(exc).__name__}: {exc}"

            proxy.record_failure(timeout="timeout" in last_error.lower())
            proxy.last_check_at = time.monotonic()
            await proxy.set_status(ProxyStatus.FAILED)
            logger.warning(
                f"[ProxyEngine] Proxy validation failed: "
                f"{proxy.url_masked} | reason={last_error}"
            )
            return False

    async def validate_all(
        self,
        concurrent: int  = 20,
        check_geo:  bool = False,
    ) -> int:
        """Validate all proxies concurrently."""
        proxies = list(self._proxies.values())
        if not proxies:
            return 0
        sem = asyncio.Semaphore(concurrent)
        async def _check(proxy: Proxy) -> bool:
            async with sem:
                return await self.validate_proxy(proxy, check_geo=check_geo)
        logger.info(
            f"[ProxyEngine] Validating {len(proxies)} proxies "
            f"(concurrent={concurrent})"
        )
        results = await asyncio.gather(
            *[_check(p) for p in proxies],
            return_exceptions=True,
        )
        healthy = sum(1 for r in results if r is True)
        logger.info(
            f"[ProxyEngine] Validation complete: "
            f"{healthy}/{len(proxies)} healthy"
        )
        await self._event_bus.publish_simple(
            EventCategory.PROXY_HEALTH_UPDATE,
            {
                "total":    len(proxies),
                "healthy":  healthy,
                "failed":   len(proxies) - healthy,
            },
        )
        return healthy

    def get_proxy(self, proxy_id: str) -> Optional[Proxy]:
        return self._proxies.get(proxy_id)

    def get_all_proxies(self) -> List[Proxy]:
        return list(self._proxies.values())

    def get_available_proxies(
        self,
        country:    Optional[str]       = None,
        proxy_type: Optional[ProxyType] = None,
    ) -> List[Proxy]:
        return self._filter_candidates(
            country=country,
            proxy_type=proxy_type,
            min_score=self._min_health_score,
        )

    def get_proxies_by_country(self, country: str) -> List[Proxy]:
        return [
            p for p in self._proxies.values()
            if p.country.upper() == country.upper()
        ]

    @property
    def total_count(self) -> int:
        return len(self._proxies)

    @property
    def available_count(self) -> int:
        return sum(1 for p in self._proxies.values() if p.is_available)

    @property
    def healthy_count(self) -> int:
        return sum(
            1 for p in self._proxies.values()
            if p.status == ProxyStatus.HEALTHY
        )

    @property
    def banned_count(self) -> int:
        return sum(
            1 for p in self._proxies.values()
            if p.status == ProxyStatus.BANNED
        )

    @property
    def socks_dependency_available(self) -> bool:
        return HAS_AIOHTTP_SOCKS

    @property
    def requires_socks_dependency(self) -> bool:
        return any(
            p.protocol in {ProxyProtocol.SOCKS4, ProxyProtocol.SOCKS5}
            for p in self._proxies.values()
        )

    def get_pool_summary(self) -> Dict[str, Any]:
        proxies = list(self._proxies.values())
        by_status = defaultdict(int)
        by_country = defaultdict(int)
        by_type = defaultdict(int)
        scores = []
        for p in proxies:
            by_status[p.status.value] += 1
            by_country[p.country or "Unknown"] += 1
            by_type[p.proxy_type.value] += 1
            scores.append(p.health.overall)
        return {
            "total":        len(proxies),
            "available":    self.available_count,
            "healthy":      self.healthy_count,
            "banned":       self.banned_count,
            "by_status":    dict(by_status),
            "by_country":   dict(sorted(by_country.items(), key=lambda x: -x[1])[:10]),
            "by_type":      dict(by_type),
            "avg_score":    round(sum(scores) / len(scores), 4) if scores else 0,
            "min_score":    round(min(scores), 4) if scores else 0,
            "max_score":    round(max(scores), 4) if scores else 0,
            "pool_stats":   self.stats.to_dict(),
        }

    def export_proxies(self, healthy_only: bool = True) -> List[str]:
        proxies = list(self._proxies.values())
        if healthy_only:
            proxies = [p for p in proxies if p.health.is_healthy]
        return [p.to_connection_string() for p in proxies]

    async def _add_proxy(self, proxy: Proxy) -> None:
        async with self._lock:
            if proxy.proxy_id not in self._proxies:
                self._proxies[proxy.proxy_id] = proxy
                self._proxy_list.append(proxy)

    def _filter_candidates(
        self,
        country:    Optional[str]       = None,
        proxy_type: Optional[ProxyType] = None,
        domain:     Optional[str]       = None,
        exclude_ids: Set[str]           = None,
        min_score:  float               = 0.0,
    ) -> List[Proxy]:
        exclude_ids = exclude_ids or set()
        candidates = []
        for proxy in self._proxy_list:
            if proxy.proxy_id in exclude_ids:
                continue
            if not proxy.is_available:
                continue
            if proxy.health.overall < min_score:
                continue
            if country and proxy.country.upper() != country.upper():
                continue
            if proxy_type and proxy.proxy_type != proxy_type:
                continue
            if domain and proxy.is_banned_for_domain(domain):
                continue
            candidates.append(proxy)
        return candidates

    def _select_proxy(
        self,
        candidates: List[Proxy],
        session_id: str,
    ) -> Proxy:
        if not candidates:
            raise ProxyPoolExhaustedError(total_proxies=0)
        strategy = self._rotation_strategy
        if strategy == RotationStrategy.ROUND_ROBIN:
            self._round_robin_idx = (
                self._round_robin_idx + 1
            ) % len(candidates)
            return candidates[self._round_robin_idx % len(candidates)]
        elif strategy == RotationStrategy.RANDOM:
            return random.choice(candidates)
        elif strategy == RotationStrategy.WEIGHTED:
            total = sum(p.weight * p.health.overall for p in candidates)
            if total == 0:
                return random.choice(candidates)
            r = random.uniform(0, total)
            cumulative = 0.0
            for proxy in candidates:
                cumulative += proxy.weight * proxy.health.overall
                if r <= cumulative:
                    return proxy
            return candidates[-1]
        elif strategy == RotationStrategy.LEAST_USED:
            return min(candidates, key=lambda p: p.health.total_requests)
        elif strategy == RotationStrategy.LEAST_FAILED:
            return min(candidates, key=lambda p: p.health.failed)
        elif strategy == RotationStrategy.PERFORMANCE:
            return max(candidates, key=lambda p: p.health.overall)
        elif strategy == RotationStrategy.GEO_OPTIMAL:
            type_priority = {
                ProxyType.RESIDENTIAL: 3,
                ProxyType.ISP:         2,
                ProxyType.MOBILE:      2,
                ProxyType.DATACENTER:  1,
                ProxyType.TOR:         0,
            }
            return max(
                candidates,
                key=lambda p: (
                    type_priority.get(p.proxy_type, 0) * 0.4 +
                    p.health.overall * 0.6
                ),
            )
        return max(candidates, key=lambda p: p.health.overall)

    async def _handle_ban(self, proxy: Proxy, domain: str) -> None:
        if domain:
            await proxy.ban(duration_seconds=self._ban_duration, domain=domain)
        else:
            await proxy.ban(duration_seconds=self._ban_duration)
        await self._event_bus.publish_simple(
            EventCategory.PROXY_BANNED,
            {
                "proxy_id": proxy.proxy_id,
                "address":  proxy.address,
                "domain":   domain,
                "duration": self._ban_duration,
            },
            priority=EventPriority.HIGH,
        )

    async def _update_proxy_status(self, proxy: Proxy) -> None:
        score = proxy.health.overall
        consecutive_failures = proxy.health.failed
        if score >= 0.7:
            await proxy.set_status(ProxyStatus.HEALTHY)
        elif score >= 0.4:
            await proxy.set_status(ProxyStatus.DEGRADED)
        elif consecutive_failures >= self._max_failures:
            await proxy.set_status(ProxyStatus.FAILED)
            await proxy.cooldown(30.0)
        else:
            await proxy.set_status(ProxyStatus.DEGRADED)

    async def _fetch_geo(
        self,
        proxy:   Proxy,
        session: aiohttp.ClientSession,
        proxy_arg: Optional[str] = None,
    ) -> None:
        for geo_url in self.GEO_CHECK_URLS:
            try:
                async with session.get(
                    geo_url,
                    proxy=proxy_arg,
                    timeout=aiohttp.ClientTimeout(total=8.0),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if data.get("status") != "fail":
                            geo = ProxyGeoInfo.from_api_response(data)
                            proxy.update_geo(geo)
                            logger.debug(
                                f"[ProxyEngine] Geo: {proxy.proxy_id} -> "
                                f"{geo.city}, {geo.country_code}"
                            )
                            return
            except Exception:
                continue

    async def _health_check_loop(self) -> None:
        logger.debug("[ProxyEngine] Health check loop started")
        await asyncio.sleep(5.0)
        while self._running:
            try:
                await self._run_health_checks()
                await asyncio.sleep(self._health_check_interval)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error(f"[ProxyEngine] Health loop error: {exc}")
                await asyncio.sleep(10.0)

    async def _run_health_checks(self) -> None:
        proxies = list(self._proxies.values())
        if not proxies:
            return
        to_check = [
            p for p in proxies
            if (time.monotonic() - p.last_check_at) > self._health_check_interval
            or p.status in {ProxyStatus.UNKNOWN, ProxyStatus.FAILED}
        ]
        if not to_check:
            return
        logger.debug(
            f"[ProxyEngine] Health checking {len(to_check)}/{len(proxies)} proxies"
        )
        results = await asyncio.gather(
            *[self.validate_proxy(p, check_geo=False) for p in to_check],
            return_exceptions=True,
        )
        healthy = sum(1 for r in results if r is True)
        failed  = sum(1 for r in results if r is False)
        await self._event_bus.publish_simple(
            EventCategory.PROXY_HEALTH_UPDATE,
            {
                "checked": len(to_check),
                "healthy": healthy,
                "failed":  failed,
                "total":   len(proxies),
            },
            priority=EventPriority.LOW,
        )
        available = self.available_count
        if available <= self._low_pool_threshold:
            await self._event_bus.publish_simple(
                EventCategory.PROXY_POOL_LOW,
                {"available": available, "total": len(proxies)},
                priority=EventPriority.HIGH,
            )

    async def _emit_pool_event(self) -> None:
        await self._event_bus.publish_simple(
            EventCategory.PROXY_HEALTH_UPDATE,
            {
                "total":     len(self._proxies),
                "available": self.available_count,
            },
        )

    def _parse_proxy_dict(
        self,
        data:       Dict[str, Any],
        proxy_type: ProxyType,
    ) -> Optional[Proxy]:
        try:
            protocol_str = str(
                data.get(
                    "protocol",
                    self._config.get("proxy.default_protocol", "http"),
                ) or "http"
            ).strip().lower()
            protocol = ProxyProtocol(protocol_str)
            return Proxy(
                host        = data["host"],
                port        = int(data["port"]),
                protocol    = protocol,
                username    = data.get("username") or data.get("user"),
                password    = data.get("password") or data.get("pass"),
                proxy_type  = ProxyType(
                    data.get("type", proxy_type.value)
                ),
                country     = data.get("country", ""),
                weight      = float(data.get("weight", 1.0)),
            )
        except Exception as exc:
            logger.debug(f"[ProxyEngine] Parse dict error: {exc}")
            return None
