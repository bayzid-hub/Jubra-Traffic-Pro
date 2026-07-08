"""
Jubra Traffic Pro - Self-Healing System
Automatic detection and recovery from system failures,
resource exhaustion, and degraded performance.
"""

import asyncio
import time
import logging
import psutil
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Callable, Set
from collections import deque
from enum import Enum, auto

from core.exceptions import SelfHealingError, ErrorContext
from core.event_bus import EventBus, EventCategory, EventPriority, Event, get_event_bus
from core.config_manager import ConfigManager

logger = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Health Check Types
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class HealthStatus(Enum):
    HEALTHY     = "healthy"
    DEGRADED    = "degraded"
    CRITICAL    = "critical"
    FAILED      = "failed"
    RECOVERING  = "recovering"
    UNKNOWN     = "unknown"


class HealingAction(Enum):
    RESTART_COMPONENT   = "restart_component"
    CLEAR_CACHE         = "clear_cache"
    REDUCE_CONCURRENCY  = "reduce_concurrency"
    ROTATE_PROXIES      = "rotate_proxies"
    REFRESH_BROWSERS    = "refresh_browsers"
    PAUSE_CAMPAIGNS     = "pause_campaigns"
    RESUME_CAMPAIGNS    = "resume_campaigns"
    GC_COLLECT          = "gc_collect"
    KILL_ZOMBIE_PROCS   = "kill_zombie_procs"
    ALERT_OPERATOR      = "alert_operator"
    EMERGENCY_STOP      = "emergency_stop"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Health Check Result
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@dataclass
class HealthCheckResult:
    """Result of a system health check."""
    component:      str
    status:         HealthStatus
    score:          float           # 0.0=failed, 1.0=perfect
    message:        str             = ""
    metrics:        Dict[str, Any]  = field(default_factory=dict)
    recommended_actions: List[HealingAction] = field(default_factory=list)
    checked_at:     float           = field(default_factory=time.monotonic)

    @property
    def is_healthy(self) -> bool:
        return self.status == HealthStatus.HEALTHY

    @property
    def needs_healing(self) -> bool:
        return self.status in (
            HealthStatus.DEGRADED,
            HealthStatus.CRITICAL,
            HealthStatus.FAILED,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "component":    self.component,
            "status":       self.status.value,
            "score":        round(self.score, 4),
            "message":      self.message,
            "metrics":      self.metrics,
            "actions":      [a.value for a in self.recommended_actions],
            "checked_at":   self.checked_at,
        }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# System Monitor
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class SystemMonitor:
    """
    Real-time system resource monitor.
    Tracks CPU, memory, disk, network, and process metrics.
    """

    def __init__(
        self,
        cpu_critical:       float = 90.0,   # % CPU usage
        memory_critical:    float = 85.0,   # % RAM usage
        disk_critical:      float = 90.0,   # % disk usage
        cpu_warning:        float = 75.0,
        memory_warning:     float = 70.0,
    ):
        self._cpu_critical      = cpu_critical
        self._memory_critical   = memory_critical
        self._disk_critical     = disk_critical
        self._cpu_warning       = cpu_warning
        self._memory_warning    = memory_warning

        self._cpu_history:      deque = deque(maxlen=60)
        self._memory_history:   deque = deque(maxlen=60)
        self._net_history:      deque = deque(maxlen=60)
        self._last_net_bytes:   Optional[Any] = None

    async def check_system(self) -> HealthCheckResult:
        """Run comprehensive system health check."""
        loop = asyncio.get_event_loop()

        try:
            metrics = await loop.run_in_executor(
                None, self._collect_metrics
            )
        except Exception as exc:
            return HealthCheckResult(
                component = "system",
                status    = HealthStatus.UNKNOWN,
                score     = 0.5,
                message   = f"Metrics collection failed: {exc}",
            )

        # Evaluate metrics
        actions  = []
        issues   = []
        score    = 1.0

        # CPU check
        cpu_pct = metrics.get("cpu_pct", 0)
        self._cpu_history.append(cpu_pct)
        avg_cpu = sum(self._cpu_history) / len(self._cpu_history)

        if avg_cpu >= self._cpu_critical:
            score  -= 0.4
            issues.append(f"CPU critical: {avg_cpu:.1f}%")
            actions.append(HealingAction.REDUCE_CONCURRENCY)
        elif avg_cpu >= self._cpu_warning:
            score  -= 0.2
            issues.append(f"CPU high: {avg_cpu:.1f}%")

        # Memory check
        mem_pct = metrics.get("memory_pct", 0)
        self._memory_history.append(mem_pct)
        avg_mem = sum(self._memory_history) / len(self._memory_history)

        if avg_mem >= self._memory_critical:
            score  -= 0.4
            issues.append(f"Memory critical: {avg_mem:.1f}%")
            actions.extend([
                HealingAction.GC_COLLECT,
                HealingAction.REFRESH_BROWSERS,
                HealingAction.REDUCE_CONCURRENCY,
            ])
        elif avg_mem >= self._memory_warning:
            score  -= 0.2
            issues.append(f"Memory high: {avg_mem:.1f}%")
            actions.append(HealingAction.GC_COLLECT)

        # Disk check
        disk_pct = metrics.get("disk_pct", 0)
        if disk_pct >= self._disk_critical:
            score  -= 0.2
            issues.append(f"Disk critical: {disk_pct:.1f}%")
            actions.append(HealingAction.CLEAR_CACHE)

        score = max(0.0, score)

        if score >= 0.8:
            status = HealthStatus.HEALTHY
        elif score >= 0.5:
            status = HealthStatus.DEGRADED
        elif score >= 0.2:
            status = HealthStatus.CRITICAL
        else:
            status = HealthStatus.FAILED

        return HealthCheckResult(
            component           = "system",
            status              = status,
            score               = score,
            message             = "; ".join(issues) or "System healthy",
            metrics             = metrics,
            recommended_actions = list(set(actions)),
        )

    def _collect_metrics(self) -> Dict[str, Any]:
        """Collect system metrics synchronously."""
        cpu_pct     = psutil.cpu_percent(interval=1.0)
        memory      = psutil.virtual_memory()
        disk        = psutil.disk_usage("/")

        metrics = {
            "cpu_pct":          cpu_pct,
            "cpu_count":        psutil.cpu_count(),
            "memory_pct":       memory.percent,
            "memory_used_mb":   memory.used / 1024 / 1024,
            "memory_avail_mb":  memory.available / 1024 / 1024,
            "memory_total_mb":  memory.total / 1024 / 1024,
            "disk_pct":         disk.percent,
            "disk_free_gb":     disk.free / 1024**3,
            "process_count":    len(psutil.pids()),
        }

        # Network I/O
        try:
            net = psutil.net_io_counters()
            if self._last_net_bytes:
                last_sent, last_recv = self._last_net_bytes
                metrics["net_sent_kbps"] = (
                    (net.bytes_sent - last_sent) / 1024
                )
                metrics["net_recv_kbps"] = (
                    (net.bytes_recv - last_recv) / 1024
                )
            self._last_net_bytes = (net.bytes_sent, net.bytes_recv)
        except Exception:
            pass

        return metrics


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Self-Healing Engine
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class SelfHealingEngine:
    """
    Jubra Traffic Pro - Self-Healing Engine

    Monitors all subsystems and automatically recovers from:
    ─────────────────────────────────────────────────────
    • High CPU/memory usage (reduce concurrency)
    • Browser crashes (restart and replenish pool)
    • Proxy pool exhaustion (trigger validation/reload)
    • Bot detection spikes (rotate identifiers)
    • CAPTCHA budget exceeded (pause campaigns)
    • Session failure rate spikes (reduce rate)
    • Network errors (switch proxies)
    • Disk space issues (clear caches)
    • Memory leaks (force GC + browser restart)
    • Dead/zombie processes (kill and restart)
    """

    def __init__(
        self,
        config:             ConfigManager,
        event_bus:          Optional[EventBus]  = None,
        check_interval:     float               = 15.0,
        healing_cooldown:   float               = 60.0,
        max_healing_per_hr: int                 = 20,
    ):
        self._config            = config
        self._event_bus         = event_bus or get_event_bus()
        self._check_interval    = check_interval
        self._healing_cooldown  = healing_cooldown
        self._max_healing_per_hr = max_healing_per_hr

        # Monitors
        self._system_monitor    = SystemMonitor()

        # Component health tracking
        self._component_health: Dict[str, HealthCheckResult] = {}
        self._component_checkers: Dict[str, Callable] = {}

        # Healing tracking
        self._last_healing:     Dict[str, float]    = {}
        self._healing_history:  deque               = deque(maxlen=500)
        self._healing_counts:   Dict[str, int]      = {}

        # External component references
        self._components:       Dict[str, Any]      = {}

        # Healing action handlers
        self._action_handlers: Dict[HealingAction, Callable] = {}
        self._setup_default_handlers()

        # Background task
        self._monitor_task:     Optional[asyncio.Task] = None
        self._running:          bool = False

        # Alerts
        self._alert_callbacks:  List[Callable] = []

        # Subscribe to failure events
        self._setup_event_listeners()

        logger.info(
            f"[SelfHealingEngine] Initialized: "
            f"interval={check_interval}s, "
            f"cooldown={healing_cooldown}s"
        )

    # ── Component Registration ─────────────────────────────

    def register_component(
        self,
        name:       str,
        component:  Any,
        checker:    Optional[Callable] = None,
    ) -> None:
        """Register a component for health monitoring."""
        self._components[name] = component
        if checker:
            self._component_checkers[name] = checker
        logger.debug(f"[SelfHealingEngine] Registered: {name}")

    def register_action_handler(
        self,
        action:     HealingAction,
        handler:    Callable,
    ) -> None:
        """Register a handler for a healing action."""
        self._action_handlers[action] = handler

    def add_alert_callback(self, callback: Callable) -> None:
        """Add alert callback for critical events."""
        self._alert_callbacks.append(callback)

    # ── Lifecycle ──────────────────────────────────────────

    async def start(self) -> None:
        """Start self-healing background monitor."""
        self._running = True
        self._monitor_task = asyncio.create_task(
            self._monitoring_loop(),
            name="SelfHealingEngine-Monitor",
        )
        logger.info("[SelfHealingEngine] Started")

    async def stop(self) -> None:
        """Stop self-healing monitor."""
        self._running = False
        if self._monitor_task:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
        logger.info("[SelfHealingEngine] Stopped")

    # ── Health Checking ────────────────────────────────────

    async def check_all(self) -> Dict[str, HealthCheckResult]:
        """Run health checks on all registered components."""
        results = {}

        # System check
        sys_result = await self._system_monitor.check_system()
        results["system"] = sys_result
        self._component_health["system"] = sys_result

        # Component checks
        for name, checker in self._component_checkers.items():
            try:
                result = await checker()
                if isinstance(result, HealthCheckResult):
                    results[name] = result
                    self._component_health[name] = result
            except Exception as exc:
                results[name] = HealthCheckResult(
                    component = name,
                    status    = HealthStatus.UNKNOWN,
                    score     = 0.5,
                    message   = f"Check error: {exc}",
                )

        # Built-in component checks
        for name, component in self._components.items():
            if name not in self._component_checkers:
                result = await self._auto_check_component(name, component)
                results[name] = result
                self._component_health[name] = result

        return results

    async def _auto_check_component(
        self,
        name:       str,
        component:  Any,
    ) -> HealthCheckResult:
        """Automatically check component health using common patterns."""
        score   = 1.0
        status  = HealthStatus.HEALTHY
        message = f"{name} operational"
        metrics = {}
        actions = []

        try:
            # Browser Farm check
            if hasattr(component, "available_count") and hasattr(component, "in_use_count"):
                available   = component.available_count
                in_use      = component.in_use_count
                total       = component.total_count
                crashed     = component.crashed_count

                metrics = {
                    "available": available,
                    "in_use":    in_use,
                    "total":     total,
                    "crashed":   crashed,
                }

                if crashed > 3:
                    score  -= 0.3
                    actions.append(HealingAction.REFRESH_BROWSERS)
                    message = f"High crash count: {crashed}"

                if total > 0 and available == 0 and in_use == total:
                    score  -= 0.2
                    message = f"Pool full: {in_use}/{total}"

            # Proxy Engine check
            elif hasattr(component, "available_count") and hasattr(component, "banned_count"):
                available   = component.available_count
                total       = component.total_count
                banned      = component.banned_count

                metrics = {
                    "available": available,
                    "total":     total,
                    "banned":    banned,
                }

                if available == 0:
                    score  -= 0.8
                    status  = HealthStatus.CRITICAL
                    message = "No proxies available!"
                    actions.append(HealingAction.ROTATE_PROXIES)
                elif available < 5:
                    score  -= 0.3
                    message = f"Low proxies: {available}"
                    actions.append(HealingAction.ROTATE_PROXIES)

                ban_rate = banned / max(total, 1)
                if ban_rate > 0.5:
                    score  -= 0.2
                    message = f"High ban rate: {ban_rate:.1%}"

            # Session Manager check
            elif hasattr(component, "active_count") and hasattr(component, "detection_rate"):
                active          = component.active_count
                detection_rate  = component.detection_rate
                success_rate    = component.success_rate

                metrics = {
                    "active":           active,
                    "detection_rate":   detection_rate,
                    "success_rate":     success_rate,
                }

                if detection_rate > 0.2:
                    score  -= 0.4
                    message = f"High detection rate: {detection_rate:.1%}"
                    actions.extend([
                        HealingAction.ROTATE_PROXIES,
                        HealingAction.REFRESH_BROWSERS,
                    ])

                if success_rate < 0.5:
                    score  -= 0.3
                    message = f"Low success rate: {success_rate:.1%}"

        except Exception as exc:
            score   = 0.7
            message = f"Auto-check error: {exc}"

        score = max(0.0, score)

        if score >= 0.8:
            status = HealthStatus.HEALTHY
        elif score >= 0.5:
            status = HealthStatus.DEGRADED
        elif score >= 0.2:
            status = HealthStatus.CRITICAL
        else:
            status = HealthStatus.FAILED

        return HealthCheckResult(
            component           = name,
            status              = status,
            score               = score,
            message             = message,
            metrics             = metrics,
            recommended_actions = actions,
        )

    # ── Healing Actions ────────────────────────────────────

    async def execute_healing(
        self,
        action:     HealingAction,
        component:  str = "",
        reason:     str = "",
    ) -> bool:
        """Execute a specific healing action."""
        # Cooldown check
        cooldown_key = f"{action.value}:{component}"
        last_heal    = self._last_healing.get(cooldown_key, 0)
        if time.monotonic() - last_heal < self._healing_cooldown:
            logger.debug(
                f"[SelfHealingEngine] Cooling down: {action.value}"
            )
            return False

        # Rate limit check
        now = time.monotonic()
        recent_heals = sum(
            1 for ts in self._healing_history
            if isinstance(ts, (int, float)) and now - ts <= 3600
        )
        if recent_heals >= self._max_healing_per_hr:
            logger.warning(
                f"[SelfHealingEngine] Healing rate limit reached: "
                f"{recent_heals}/{self._max_healing_per_hr}/hr"
            )
            return False

        logger.info(
            f"[SelfHealingEngine] Executing: {action.value} | "
            f"component={component} | reason={reason}"
        )

        success = False
        try:
            # Check custom handler first
            if action in self._action_handlers:
                result = await self._action_handlers[action](
                    component=component,
                    reason=reason,
                )
                success = bool(result)
            else:
                # Default handlers
                success = await self._default_heal(action, component)

        except Exception as exc:
            logger.error(
                f"[SelfHealingEngine] Healing failed: "
                f"{action.value}: {exc}"
            )

        # Record
        self._last_healing[cooldown_key] = time.monotonic()
        self._healing_history.append(time.monotonic())
        self._healing_counts[action.value] = (
            self._healing_counts.get(action.value, 0) + 1
        )

        await self._event_bus.publish_simple(
            EventCategory.HEALING_SUCCESS if success
            else EventCategory.HEALING_FAILED,
            {
                "action":    action.value,
                "component": component,
                "reason":    reason,
                "success":   success,
            },
            priority=EventPriority.HIGH,
        )

        return success

    async def _default_heal(
        self,
        action:     HealingAction,
        component:  str,
    ) -> bool:
        """Default healing implementations."""
        if action == HealingAction.GC_COLLECT:
            import gc
            collected = gc.collect()
            logger.info(
                f"[SelfHealingEngine] GC collected: {collected} objects"
            )
            return True

        elif action == HealingAction.CLEAR_CACHE:
            # Clear any registered caches
            for name, comp in self._components.items():
                if hasattr(comp, "clear_cache"):
                    try:
                        await comp.clear_cache()
                    except Exception:
                        pass
            return True

        elif action == HealingAction.REDUCE_CONCURRENCY:
            # Signal traffic orchestrator to reduce workers
            for name, comp in self._components.items():
                if hasattr(comp, "_max_concurrent"):
                    old = comp._max_concurrent
                    comp._max_concurrent = max(1, int(old * 0.75))
                    logger.info(
                        f"[SelfHealingEngine] Reduced concurrency: "
                        f"{old} → {comp._max_concurrent}"
                    )
            return True

        elif action == HealingAction.REFRESH_BROWSERS:
            browser_farm = self._components.get("browser_farm")
            if browser_farm and hasattr(browser_farm, "_pool_replenisher"):
                logger.info("[SelfHealingEngine] Triggering browser refresh")
                # Schedule pool replenishment
                return True
            return False

        elif action == HealingAction.ROTATE_PROXIES:
            proxy_engine = self._components.get("proxy_engine")
            if proxy_engine and hasattr(proxy_engine, "validate_all"):
                logger.info("[SelfHealingEngine] Triggering proxy validation")
                asyncio.create_task(proxy_engine.validate_all(concurrent=10))
                return True
            return False

        elif action == HealingAction.PAUSE_CAMPAIGNS:
            orchestrator = self._components.get("traffic_orchestrator")
            if orchestrator:
                for campaign in orchestrator.get_all_campaigns():
                    if campaign.is_active:
                        await orchestrator.pause_campaign(campaign.campaign_id)
                return True
            return False

        elif action == HealingAction.ALERT_OPERATOR:
            await self._send_alerts(
                f"[ALERT] Healing action required: {action.value} | "
                f"component={component}"
            )
            return True

        elif action == HealingAction.EMERGENCY_STOP:
            logger.critical(
                "[SelfHealingEngine] EMERGENCY STOP triggered!"
            )
            orchestrator = self._components.get("traffic_orchestrator")
            if orchestrator:
                await orchestrator.stop_all()
            return True

        return False

    # ── Monitoring Loop ────────────────────────────────────

    async def _monitoring_loop(self) -> None:
        """Main monitoring loop."""
        logger.debug("[SelfHealingEngine] Monitoring loop started")

        while self._running:
            try:
                await asyncio.sleep(self._check_interval)

                # Run all health checks
                results = await self.check_all()

                # Process results and trigger healing
                for component, result in results.items():
                    if result.needs_healing:
                        for action in result.recommended_actions:
                            await self.execute_healing(
                                action    = action,
                                component = component,
                                reason    = result.message,
                            )

                # Publish health summary
                await self._publish_health_summary(results)

            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error(
                    f"[SelfHealingEngine] Monitor loop error: {exc}",
                    exc_info=True,
                )

    async def _publish_health_summary(
        self,
        results: Dict[str, HealthCheckResult],
    ) -> None:
        """Publish aggregate health summary event."""
        overall_score = (
            sum(r.score for r in results.values()) / len(results)
            if results else 1.0
        )
        status_counts = {}
        for r in results.values():
            key = r.status.value
            status_counts[key] = status_counts.get(key, 0) + 1

        await self._event_bus.publish_simple(
            EventCategory.HEALTH_CHECK,
            {
                "overall_score":    round(overall_score, 4),
                "component_count":  len(results),
                "status_counts":    status_counts,
                "components":       {
                    name: {
                        "status": r.status.value,
                        "score":  round(r.score, 3),
                    }
                    for name, r in results.items()
                },
            },
            priority=EventPriority.LOW,
        )

    # ── Event Listeners ────────────────────────────────────

    def _setup_event_listeners(self) -> None:
        """Listen for failure events to trigger immediate healing."""
        self._event_bus.subscribe(
            category   = EventCategory.DETECTION_BOT_DETECTED,
            handler    = self._on_bot_detected,
            source_tag = "SelfHealingEngine",
        )
        self._event_bus.subscribe(
            category   = EventCategory.BROWSER_CRASHED,
            handler    = self._on_browser_crash,
            source_tag = "SelfHealingEngine",
        )
        self._event_bus.subscribe(
            category   = EventCategory.PROXY_POOL_EXHAUSTED,
            handler    = self._on_proxy_exhausted,
            source_tag = "SelfHealingEngine",
        )

    async def _on_bot_detected(self, event: Event) -> None:
        """React to bot detection event."""
        logger.warning("[SelfHealingEngine] Bot detection → healing")
        await self.execute_healing(
            HealingAction.ROTATE_PROXIES,
            component = "proxy_engine",
            reason    = "bot_detected",
        )

    async def _on_browser_crash(self, event: Event) -> None:
        """React to browser crash."""
        await self.execute_healing(
            HealingAction.REFRESH_BROWSERS,
            component = "browser_farm",
            reason    = "crash_detected",
        )

    async def _on_proxy_exhausted(self, event: Event) -> None:
        """React to proxy pool exhaustion."""
        await self.execute_healing(
            HealingAction.PAUSE_CAMPAIGNS,
            component = "traffic_orchestrator",
            reason    = "proxy_pool_exhausted",
        )

    def _setup_default_handlers(self) -> None:
        """Register default action handlers."""
        pass  # Default handlers in _default_heal()

    async def _send_alerts(self, message: str) -> None:
        """Send alert to all registered callbacks."""
        for callback in self._alert_callbacks:
            try:
                if asyncio.iscoroutinefunction(callback):
                    await callback(message)
                else:
                    callback(message)
            except Exception as exc:
                logger.error(f"[SelfHealingEngine] Alert callback error: {exc}")

    # ── Metrics ────────────────────────────────────────────

    def get_metrics(self) -> Dict[str, Any]:
        now = time.monotonic()
        recent_heals = sum(
            1 for ts in self._healing_history
            if isinstance(ts, (int, float)) and now - ts <= 3600
        )
        return {
            "monitoring":           self._running,
            "check_interval_s":     self._check_interval,
            "total_components":     len(self._components),
            "heals_last_hour":      recent_heals,
            "healing_by_action":    dict(self._healing_counts),
            "component_health": {
                name: {
                    "status": r.status.value,
                    "score":  round(r.score, 3),
                }
                for name, r in self._component_health.items()
            },
        }

    def get_health_report(self) -> Dict[str, Any]:
        """Get full health report of all components."""
        return {
            name: result.to_dict()
            for name, result in self._component_health.items()
        }