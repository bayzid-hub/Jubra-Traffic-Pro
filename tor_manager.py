"""
Jubra Traffic Pro - Tor Circuit Manager
Advanced Tor integration with circuit renewal, identity rotation,
bridge support, and real-time circuit health monitoring.
"""

import asyncio
import time
import random
import logging
import hashlib
import socket
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple
from collections import deque
from enum import Enum, auto

try:
    import stem
    import stem.control
    import stem.process
    import stem.util.log
    from stem import Signal
    from stem.control import Controller
    HAS_STEM = True
except ImportError:
    HAS_STEM = False
    logging.warning(
        "[TorManager] stem library not installed. "
        "Install with: pip install stem"
    )

import aiohttp

from core.exceptions import TorCircuitError, ErrorContext
from core.event_bus import EventBus, EventCategory, EventPriority, get_event_bus
from core.config_manager import ConfigManager

logger = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Circuit Status
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class CircuitStatus(Enum):
    BUILDING  = "building"
    BUILT     = "built"
    FAILED    = "failed"
    CLOSED    = "closed"
    RENEWING  = "renewing"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Circuit Info
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@dataclass
class TorCircuit:
    """Represents a single Tor circuit with its nodes and metrics."""
    circuit_id:     str
    status:         CircuitStatus
    path:           List[str]           # List of relay fingerprints
    exit_node:      str                 # Exit node fingerprint
    exit_country:   str                 # Exit node country code
    exit_ip:        str                 # Resolved exit IP
    built_at:       float               = field(default_factory=time.monotonic)
    last_used_at:   float               = field(default_factory=time.monotonic)
    request_count:  int                 = 0
    failure_count:  int                 = 0
    latency_ms:     float               = 0.0
    is_active:      bool                = True

    @property
    def age_seconds(self) -> float:
        return time.monotonic() - self.built_at

    @property
    def idle_seconds(self) -> float:
        return time.monotonic() - self.last_used_at

    @property
    def success_rate(self) -> float:
        total = self.request_count + self.failure_count
        if total == 0:
            return 1.0
        return self.request_count / total

    @property
    def is_healthy(self) -> bool:
        return (
            self.status == CircuitStatus.BUILT
            and self.is_active
            and self.success_rate > 0.5
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "circuit_id":    self.circuit_id,
            "status":        self.status.value,
            "exit_country":  self.exit_country,
            "exit_ip":       self.exit_ip,
            "age_seconds":   round(self.age_seconds, 1),
            "idle_seconds":  round(self.idle_seconds, 1),
            "requests":      self.request_count,
            "failures":      self.failure_count,
            "success_rate":  round(self.success_rate, 4),
            "latency_ms":    round(self.latency_ms, 2),
            "is_healthy":    self.is_healthy,
        }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Tor Identity Pool
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TorIdentityPool:
    """
    Manages a pool of Tor identities (circuits).
    Each identity has its own circuit and exit IP.
    Supports pre-warming circuits for faster acquisition.
    """

    def __init__(self, max_size: int = 10):
        self._circuits:     Dict[str, TorCircuit]   = {}
        self._available:    List[str]               = []
        self._in_use:       Set[str]                = set()
        self._max_size      = max_size
        self._lock          = asyncio.Lock()

    async def add_circuit(self, circuit: TorCircuit) -> None:
        async with self._lock:
            self._circuits[circuit.circuit_id] = circuit
            if circuit.is_healthy:
                self._available.append(circuit.circuit_id)

    async def acquire(self) -> Optional[TorCircuit]:
        """Get an available circuit from the pool."""
        async with self._lock:
            while self._available:
                cid = self._available.pop(0)
                circuit = self._circuits.get(cid)
                if circuit and circuit.is_healthy:
                    self._in_use.add(cid)
                    circuit.last_used_at = time.monotonic()
                    return circuit
            return None

    async def release(
        self,
        circuit_id: str,
        success:    bool = True,
    ) -> None:
        """Return circuit to available pool."""
        async with self._lock:
            self._in_use.discard(circuit_id)
            circuit = self._circuits.get(circuit_id)
            if circuit:
                if success:
                    circuit.request_count += 1
                else:
                    circuit.failure_count += 1

                if circuit.is_healthy:
                    self._available.append(circuit_id)

    async def remove(self, circuit_id: str) -> None:
        """Remove a circuit from the pool."""
        async with self._lock:
            self._circuits.pop(circuit_id, None)
            self._available = [
                c for c in self._available if c != circuit_id
            ]
            self._in_use.discard(circuit_id)

    async def get_expired(self, max_age_seconds: float) -> List[TorCircuit]:
        """Get circuits that have exceeded max age."""
        async with self._lock:
            return [
                c for c in self._circuits.values()
                if c.age_seconds > max_age_seconds
            ]

    async def get_all(self) -> List[TorCircuit]:
        async with self._lock:
            return list(self._circuits.values())

    @property
    def size(self) -> int:
        return len(self._circuits)

    @property
    def available_count(self) -> int:
        return len(self._available)

    @property
    def in_use_count(self) -> int:
        return len(self._in_use)

    def is_full(self) -> bool:
        return len(self._circuits) >= self._max_size


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Tor Manager
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TorManager:
    """
    Jubra Traffic Pro - Tor Circuit Manager

    Features:
    ─────────────────────────────────────────────────────
    • Automated circuit creation and renewal
    • Circuit health monitoring and replacement
    • Multiple simultaneous identities (circuit pool)
    • Exit node country filtering
    • Bridge support for censored environments
    • Stream isolation per session
    • NEWNYM signal for identity rotation
    • Circuit pre-warming for low-latency acquisition
    • Transparent proxy mode (SOCKS5 port management)
    • Real-time exit IP tracking
    • Auto-recovery from Tor service failures
    • Bandwidth and latency monitoring
    """

    TOR_SOCKS_PORT      = 9050
    TOR_CONTROL_PORT    = 9051
    TOR_DEFAULT_HOST    = "127.0.0.1"

    # Test URLs for circuit validation
    CIRCUIT_TEST_URLS = [
        "https://check.torproject.org/api/ip",
        "https://api.ipify.org?format=json",
        "https://httpbin.org/ip",
    ]

    def __init__(
        self,
        config:                 ConfigManager,
        event_bus:              Optional[EventBus] = None,
        control_host:           str               = "127.0.0.1",
        control_port:           int               = 9051,
        control_password:       str               = "",
        socks_host:             str               = "127.0.0.1",
        socks_port:             int               = 9050,
        circuit_ttl:            float             = 600.0,
        circuit_pool_size:      int               = 5,
        max_circuit_failures:   int               = 3,
        exit_countries:         Optional[List[str]] = None,
        use_bridges:            bool              = False,
        bridges:                Optional[List[str]] = None,
        stream_isolation:       bool              = True,
        pre_warm_circuits:      int               = 2,
    ):
        self._config                = config
        self._event_bus             = event_bus or get_event_bus()
        self._control_host          = control_host
        self._control_port          = control_port
        self._control_password      = control_password
        self._socks_host            = socks_host
        self._socks_port            = socks_port
        self._circuit_ttl           = circuit_ttl
        self._circuit_pool_size     = circuit_pool_size
        self._max_circuit_failures  = max_circuit_failures
        self._exit_countries        = [c.upper() for c in (exit_countries or [])]
        self._use_bridges           = use_bridges
        self._bridges               = bridges or []
        self._stream_isolation      = stream_isolation
        self._pre_warm_count        = pre_warm_circuits

        # Core state
        self._controller:   Optional["Controller"] = None
        self._circuit_pool  = TorIdentityPool(circuit_pool_size)
        self._running:      bool  = False
        self._connected:    bool  = False
        self._current_ip:   str   = ""
        self._lock          = asyncio.Lock()

        # Metrics
        self._total_renewals:   int   = 0
        self._total_circuits:   int   = 0
        self._failed_circuits:  int   = 0
        self._renewal_times:    deque = deque(maxlen=100)
        self._start_time:       float = time.monotonic()

        # Background tasks
        self._renewal_task: Optional[asyncio.Task] = None
        self._monitor_task: Optional[asyncio.Task] = None

        logger.info(
            f"[TorManager] Initialized: "
            f"control={control_host}:{control_port}, "
            f"socks={socks_host}:{socks_port}, "
            f"ttl={circuit_ttl}s, pool={circuit_pool_size}"
        )

    # ── Lifecycle ──────────────────────────────────────────

    async def start(self) -> None:
        """Connect to Tor control port and start circuit manager."""
        if not HAS_STEM:
            raise TorCircuitError(
                reason="stem library not installed. pip install stem",
                context=ErrorContext(module="TorManager", operation="start"),
            )

        self._running = True
        await self._connect_controller()

        # Pre-warm circuit pool
        if self._pre_warm_count > 0:
            logger.info(
                f"[TorManager] Pre-warming {self._pre_warm_count} circuits..."
            )
            for _ in range(self._pre_warm_count):
                try:
                    await self._build_circuit()
                except Exception as exc:
                    logger.warning(f"[TorManager] Pre-warm error: {exc}")

        # Start background tasks
        self._renewal_task = asyncio.create_task(
            self._circuit_renewal_loop(),
            name="TorManager-CircuitRenewal",
        )
        self._monitor_task = asyncio.create_task(
            self._circuit_monitor_loop(),
            name="TorManager-CircuitMonitor",
        )

        logger.info(
            f"[TorManager] Started: "
            f"ip={self._current_ip}, "
            f"pool={self._circuit_pool.size}"
        )

    async def stop(self) -> None:
        """Stop Tor manager and close all circuits."""
        self._running = False

        if self._renewal_task:
            self._renewal_task.cancel()
            try:
                await self._renewal_task
            except asyncio.CancelledError:
                pass

        if self._monitor_task:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass

        if self._controller:
            try:
                self._controller.close()
            except Exception:
                pass
            self._controller = None

        self._connected = False
        logger.info("[TorManager] Stopped")

    # ── Public API ─────────────────────────────────────────

    async def acquire_circuit(
        self,
        session_id:     str,
        country:        Optional[str] = None,
    ) -> Optional[TorCircuit]:
        """
        Acquire a Tor circuit for a session.
        Returns a circuit from the pool or builds a new one.
        """
        if not self._connected:
            await self._connect_controller()

        # Try pool first
        circuit = await self._circuit_pool.acquire()
        if circuit:
            logger.debug(
                f"[TorManager] Circuit acquired from pool: "
                f"{circuit.circuit_id} ({circuit.exit_country})"
            )
            return circuit

        # Build new circuit
        try:
            circuit = await self._build_circuit(exit_country=country)
            if circuit:
                circuit.last_used_at = time.monotonic()
                await self._circuit_pool._in_use.add(circuit.circuit_id)
                return circuit
        except Exception as exc:
            logger.error(f"[TorManager] Circuit build failed: {exc}")

        return None

    async def release_circuit(
        self,
        circuit_id: str,
        success:    bool = True,
    ) -> None:
        """Return circuit to pool."""
        await self._circuit_pool.release(circuit_id, success)

    async def renew_identity(self, session_id: str = "") -> str:
        """
        Request a new Tor identity (NEWNYM signal).
        Returns the new exit IP after renewal.
        """
        async with self._lock:
            if not self._connected or not self._controller:
                await self._connect_controller()

            start = time.monotonic()
            try:
                # Send NEWNYM signal
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(
                    None,
                    self._controller.signal,
                    Signal.NEWNYM,
                )

                # Wait for new circuit to build
                await asyncio.sleep(5.0)

                # Verify new IP
                new_ip = await self._get_exit_ip()
                elapsed = time.monotonic() - start
                self._renewal_times.append(elapsed * 1000)
                self._total_renewals += 1
                self._current_ip = new_ip

                await self._event_bus.publish_simple(
                    EventCategory.PROXY_ROTATED,
                    {
                        "type":       "tor_renewal",
                        "new_ip":     new_ip,
                        "session_id": session_id,
                        "elapsed_ms": round(elapsed * 1000, 2),
                    },
                    priority=EventPriority.NORMAL,
                )

                logger.info(
                    f"[TorManager] Identity renewed: "
                    f"ip={new_ip} ({elapsed:.1f}s)"
                )
                return new_ip

            except Exception as exc:
                raise TorCircuitError(
                    reason=f"NEWNYM failed: {exc}",
                    context=ErrorContext(
                        module="TorManager",
                        operation="renew_identity",
                        session_id=session_id,
                    ),
                ) from exc

    async def build_circuit_to_country(
        self,
        country_code: str,
    ) -> Optional[TorCircuit]:
        """Build a circuit with a specific exit node country."""
        return await self._build_circuit(exit_country=country_code.upper())

    @property
    def socks_proxy_url(self) -> str:
        """SOCKS5 proxy URL for browser/requests configuration."""
        return f"socks5://{self._socks_host}:{self._socks_port}"

    @property
    def current_ip(self) -> str:
        return self._current_ip

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def circuit_count(self) -> int:
        return self._circuit_pool.size

    def get_status(self) -> Dict[str, Any]:
        avg_renewal = (
            sum(self._renewal_times) / len(self._renewal_times)
            if self._renewal_times else 0
        )
        return {
            "connected":         self._connected,
            "current_ip":        self._current_ip,
            "circuit_pool_size": self._circuit_pool.size,
            "available_circuits": self._circuit_pool.available_count,
            "in_use_circuits":   self._circuit_pool.in_use_count,
            "total_renewals":    self._total_renewals,
            "total_circuits":    self._total_circuits,
            "failed_circuits":   self._failed_circuits,
            "avg_renewal_ms":    round(avg_renewal, 2),
            "exit_countries":    self._exit_countries,
            "socks_url":         self.socks_proxy_url,
            "uptime_seconds":    round(time.monotonic() - self._start_time, 1),
        }

    # ── Internal Circuit Building ──────────────────────────

    async def _build_circuit(
        self,
        exit_country: Optional[str] = None,
    ) -> Optional[TorCircuit]:
        """Build a new Tor circuit through the controller."""
        if not self._connected or not self._controller:
            await self._connect_controller()

        try:
            # Build circuit using stem
            if exit_country:
                # Request specific exit country
                loop = asyncio.get_event_loop()
                circuit_id = await loop.run_in_executor(
                    None,
                    self._build_circuit_sync,
                    exit_country,
                )
            else:
                loop = asyncio.get_event_loop()
                circuit_id = await loop.run_in_executor(
                    None,
                    self._build_circuit_sync,
                    None,
                )

            if not circuit_id:
                self._failed_circuits += 1
                return None

            # Wait for circuit to be built
            await asyncio.sleep(3.0)

            # Get exit IP
            exit_ip = await self._get_exit_ip_via_circuit(circuit_id)

            # Create circuit object
            circuit = TorCircuit(
                circuit_id   = str(circuit_id),
                status       = CircuitStatus.BUILT,
                path         = [],
                exit_node    = "",
                exit_country = exit_country or self._detect_country(exit_ip),
                exit_ip      = exit_ip,
            )

            # Validate circuit
            latency = await self._measure_circuit_latency(exit_ip)
            circuit.latency_ms = latency

            self._total_circuits += 1
            await self._circuit_pool.add_circuit(circuit)

            logger.debug(
                f"[TorManager] Circuit built: {circuit_id} | "
                f"country={circuit.exit_country} | "
                f"ip={exit_ip} | latency={latency:.0f}ms"
            )

            return circuit

        except Exception as exc:
            self._failed_circuits += 1
            logger.error(f"[TorManager] Build circuit error: {exc}")
            raise TorCircuitError(
                reason=str(exc),
                context=ErrorContext(
                    module="TorManager",
                    operation="_build_circuit",
                ),
            ) from exc

    def _build_circuit_sync(
        self,
        exit_country: Optional[str],
    ) -> Optional[str]:
        """Synchronous circuit building (run in executor)."""
        try:
            if not self._controller:
                return None

            if exit_country and exit_country in self._exit_countries:
                # Use ExitNodes configuration for country selection
                self._controller.set_options({
                    "ExitNodes": f"{{{exit_country}}}",
                    "StrictNodes": "1",
                })

            # Build new circuit
            circuit_id = self._controller.new_circuit(
                await_build=True,
            )
            return circuit_id

        except Exception as exc:
            logger.error(f"[TorManager] Sync circuit build: {exc}")
            return None

    # ── Controller Connection ──────────────────────────────

    async def _connect_controller(self) -> None:
        """Connect to Tor control port."""
        if not HAS_STEM:
            raise TorCircuitError(
                reason="stem not installed",
                context=ErrorContext(module="TorManager"),
            )

        for attempt in range(3):
            try:
                loop = asyncio.get_event_loop()
                controller = await loop.run_in_executor(
                    None,
                    self._connect_controller_sync,
                )
                self._controller = controller
                self._connected  = True

                # Get current IP
                self._current_ip = await self._get_exit_ip()

                logger.info(
                    f"[TorManager] Connected to control port: "
                    f"{self._control_host}:{self._control_port} | "
                    f"ip={self._current_ip}"
                )
                return

            except Exception as exc:
                logger.warning(
                    f"[TorManager] Connection attempt {attempt + 1}/3 failed: {exc}"
                )
                if attempt < 2:
                    await asyncio.sleep(2.0 * (attempt + 1))

        raise TorCircuitError(
            reason=f"Failed to connect to Tor control port "
                   f"{self._control_host}:{self._control_port} after 3 attempts",
            context=ErrorContext(module="TorManager", operation="_connect_controller"),
        )

    def _connect_controller_sync(self) -> "Controller":
        """Synchronous controller connection."""
        controller = Controller.from_port(
            address=self._control_host,
            port=self._control_port,
        )
        controller.authenticate(password=self._control_password or None)
        return controller

    # ── IP Resolution ──────────────────────────────────────

    async def _get_exit_ip(self) -> str:
        """Get current Tor exit IP."""
        try:
            connector = aiohttp.TCPConnector(ssl=False)
            timeout   = aiohttp.ClientTimeout(total=10.0)

            async with aiohttp.ClientSession(
                connector=connector,
                timeout=timeout,
            ) as session:
                for url in self.CIRCUIT_TEST_URLS:
                    try:
                        async with session.get(
                            url,
                            proxy=self.socks_proxy_url,
                        ) as resp:
                            if resp.status == 200:
                                data = await resp.json()
                                ip = (
                                    data.get("ip") or
                                    data.get("IsTor") and data.get("IP") or
                                    data.get("origin", "").split(",")[0].strip()
                                )
                                if ip:
                                    return ip
                    except Exception:
                        continue

        except Exception as exc:
            logger.debug(f"[TorManager] IP fetch error: {exc}")

        return "unknown"

    async def _get_exit_ip_via_circuit(self, circuit_id: str) -> str:
        """Get exit IP for a specific circuit."""
        # For now, use general exit IP
        # In production, use stream attachment to specific circuit
        return await self._get_exit_ip()

    def _detect_country(self, ip: str) -> str:
        """Detect country from IP (simplified)."""
        # In production, use GeoIP database
        return "XX"

    async def _measure_circuit_latency(self, exit_ip: str) -> float:
        """Measure circuit latency."""
        try:
            start = time.monotonic()
            connector = aiohttp.TCPConnector(ssl=False)
            timeout   = aiohttp.ClientTimeout(total=8.0)

            async with aiohttp.ClientSession(
                connector=connector, timeout=timeout
            ) as session:
                async with session.get(
                    "https://httpbin.org/ip",
                    proxy=self.socks_proxy_url,
                ) as resp:
                    await resp.text()
                    return (time.monotonic() - start) * 1000
        except Exception:
            return 0.0

    # ── Background Loops ───────────────────────────────────

    async def _circuit_renewal_loop(self) -> None:
        """Periodically renew circuits that have exceeded TTL."""
        logger.debug("[TorManager] Circuit renewal loop started")

        while self._running:
            try:
                await asyncio.sleep(self._circuit_ttl / 2)

                if not self._connected:
                    continue

                # Get expired circuits
                expired = await self._circuit_pool.get_expired(self._circuit_ttl)
                for circuit in expired:
                    logger.info(
                        f"[TorManager] Renewing expired circuit: "
                        f"{circuit.circuit_id} (age={circuit.age_seconds:.0f}s)"
                    )
                    await self._circuit_pool.remove(circuit.circuit_id)

                    try:
                        new_circuit = await self._build_circuit(
                            exit_country=circuit.exit_country or None
                        )
                        if new_circuit:
                            logger.debug(
                                f"[TorManager] Renewed: "
                                f"{circuit.circuit_id} → {new_circuit.circuit_id}"
                            )
                    except Exception as exc:
                        logger.warning(
                            f"[TorManager] Renewal failed: {exc}"
                        )

                # Replenish pool if needed
                while (
                    self._circuit_pool.size < self._circuit_pool_size
                    and not self._circuit_pool.is_full()
                ):
                    try:
                        country = (
                            random.choice(self._exit_countries)
                            if self._exit_countries
                            else None
                        )
                        await self._build_circuit(exit_country=country)
                    except Exception as exc:
                        logger.warning(
                            f"[TorManager] Pool replenish failed: {exc}"
                        )
                        break

            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error(f"[TorManager] Renewal loop error: {exc}")
                await asyncio.sleep(10.0)

    async def _circuit_monitor_loop(self) -> None:
        """Monitor circuit health and detect connection issues."""
        while self._running:
            try:
                await asyncio.sleep(30.0)

                # Check controller connection
                if not self._connected:
                    logger.warning("[TorManager] Controller disconnected, reconnecting...")
                    try:
                        await self._connect_controller()
                    except Exception as exc:
                        logger.error(f"[TorManager] Reconnect failed: {exc}")
                        continue

                # Verify current IP
                current_ip = await self._get_exit_ip()
                if current_ip and current_ip != "unknown":
                    self._current_ip = current_ip

                # Check circuit pool health
                all_circuits = await self._circuit_pool.get_all()
                unhealthy = [c for c in all_circuits if not c.is_healthy]

                for circuit in unhealthy:
                    logger.warning(
                        f"[TorManager] Unhealthy circuit: "
                        f"{circuit.circuit_id} "
                        f"(failures={circuit.failure_count})"
                    )
                    await self._circuit_pool.remove(circuit.circuit_id)

                await self._event_bus.publish_simple(
                    EventCategory.HEALTH_CHECK,
                    {
                        "component":   "tor_manager",
                        "connected":   self._connected,
                        "current_ip":  self._current_ip,
                        "pool_size":   self._circuit_pool.size,
                        "unhealthy":   len(unhealthy),
                    },
                    priority=EventPriority.LOW,
                )

            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error(f"[TorManager] Monitor loop error: {exc}")
                await asyncio.sleep(10.0)

    # ── Bridge Support ─────────────────────────────────────

    async def configure_bridges(self, bridges: List[str]) -> bool:
        """
        Configure Tor bridges for censored environments.
        Supports obfs4, meek, and snowflake bridges.
        """
        if not self._connected or not self._controller:
            return False

        try:
            loop = asyncio.get_event_loop()

            def _set_bridges():
                self._controller.set_options({
                    "UseBridges": "1",
                    "Bridge": bridges,
                })

            await loop.run_in_executor(None, _set_bridges)
            self._bridges = bridges
            logger.info(
                f"[TorManager] Configured {len(bridges)} bridges"
            )
            return True
        except Exception as exc:
            logger.error(f"[TorManager] Bridge config error: {exc}")
            return False

    # ── Stream Isolation ───────────────────────────────────

    def get_isolated_socks_url(self, session_id: str) -> str:
        """
        Get a SOCKS5 URL with stream isolation for a session.
        Different credentials = different Tor circuit.
        """
        if not self._stream_isolation:
            return self.socks_proxy_url

        # Use session_id as username for stream isolation
        # Tor treats different username:password as needing separate circuits
        iso_user = hashlib.md5(session_id.encode()).hexdigest()[:8]
        iso_pass = hashlib.md5((session_id + "v6").encode()).hexdigest()[:8]

        return (
            f"socks5://{iso_user}:{iso_pass}"
            f"@{self._socks_host}:{self._socks_port}"
        )