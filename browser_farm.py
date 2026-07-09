"""
Jubra Traffic Pro - Browser Farm (nodriver Edition)
Pre-warmed browser pool using nodriver - no chromedriver needed.
"""

import asyncio
import time
import uuid
import logging
import random
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Callable
from collections import deque
from contextlib import asynccontextmanager

try:
    import nodriver as uc
    HAS_NODRIVER = True
except ImportError:
    HAS_NODRIVER = False

from engines.browser.browser_controller import (
    BrowserInstance,
    BrowserProfile,
    BrowserState,
)
from core.exceptions import (
    BrowserLaunchError,
    BrowserCrashedError,
    BrowserPoolExhaustedError,
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


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Farm Metrics
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class FarmMetrics:
    """Real-time metrics for browser farm."""

    def __init__(self):
        self.total_launched:        int     = 0
        self.total_destroyed:       int     = 0
        self.total_crashed:         int     = 0
        self.total_recycled:        int     = 0
        self.total_acquisitions:    int     = 0
        self.total_releases:        int     = 0
        self.total_warmup_failures: int     = 0
        self._acquisition_times:    deque   = deque(maxlen=500)
        self._session_durations:    deque   = deque(maxlen=500)
        self._memory_samples:       deque   = deque(maxlen=200)
        self._start_time:           float   = time.monotonic()

    def record_acquisition(self, wait_ms: float) -> None:
        self.total_acquisitions += 1
        self._acquisition_times.append(wait_ms)

    def record_release(self, duration_s: float) -> None:
        self.total_releases += 1
        self._session_durations.append(duration_s)

    def record_memory(self, total_mb: float) -> None:
        self._memory_samples.append((time.monotonic(), total_mb))

    @property
    def avg_acquisition_ms(self) -> float:
        if not self._acquisition_times:
            return 0.0
        return sum(self._acquisition_times) / len(self._acquisition_times)

    @property
    def avg_session_duration(self) -> float:
        if not self._session_durations:
            return 0.0
        return sum(self._session_durations) / len(self._session_durations)

    @property
    def crash_rate(self) -> float:
        if self.total_launched == 0:
            return 0.0
        return self.total_crashed / self.total_launched

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total_launched":       self.total_launched,
            "total_destroyed":      self.total_destroyed,
            "total_crashed":        self.total_crashed,
            "total_recycled":       self.total_recycled,
            "total_acquisitions":   self.total_acquisitions,
            "avg_acquisition_ms":   round(self.avg_acquisition_ms, 2),
            "avg_session_duration": round(self.avg_session_duration, 2),
            "crash_rate":           round(self.crash_rate, 4),
            "uptime_s":             round(
                time.monotonic() - self._start_time, 1
            ),
        }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Browser Farm
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class BrowserFarm:
    """
    Jubra Traffic Pro - Browser Farm (nodriver Edition)

    Key improvements over Selenium version:
    ─────────────────────────────────────────────────────
    • No ChromeDriver needed - zero version conflicts
    • Native async launch and control
    • Direct CDP access per browser
    • Lower memory per browser instance
    • Faster startup (no driver handshake)
    • Better crash detection via process monitoring
    """

    def __init__(
        self,
        config:                 ConfigManager,
        event_bus:              Optional[EventBus]  = None,
        pool_size:              int                 = 5,
        min_available:          int                 = 2,
        max_memory_mb:          float               = 4096.0,
        warmup_count:           int                 = 2,
        recycle_after:          int                 = 50,
        crash_recovery:         bool                = True,
        health_check_interval:  float               = 30.0,
        acquisition_timeout:    float               = 60.0,
        profile_factory:        Optional[Callable]  = None,
    ):
        self._config                = config
        self._event_bus             = event_bus or get_event_bus()
        self._pool_size             = pool_size
        self._min_available         = min_available
        self._max_memory_mb         = max_memory_mb
        self._warmup_count          = warmup_count
        self._recycle_after         = recycle_after
        self._crash_recovery        = crash_recovery
        self._health_check_interval = health_check_interval
        self._acquisition_timeout   = acquisition_timeout
        self._profile_factory       = profile_factory

        # Pool storage
        self._available:    asyncio.Queue               = asyncio.Queue()
        self._all:          Dict[str, BrowserInstance]  = {}
        self._in_use:       Dict[str, BrowserInstance]  = {}
        self._crashed:      Dict[str, BrowserInstance]  = {}
        self._pool_lock     = asyncio.Lock()

        # Metrics
        self.metrics        = FarmMetrics()

        # Background tasks
        self._health_task:  Optional[asyncio.Task]  = None
        self._warmup_task:  Optional[asyncio.Task]  = None
        self._running:      bool                    = False

        # Semaphore: configurable hard cap for simultaneous browser launches.
        # Keep this at 1 by default to prevent multiple foreground windows from
        # opening at the same time when the system is under failure/retry load.
        self._launch_sem    = asyncio.Semaphore(
            int(config.get("browser.max_parallel_launches", 1))
        )
        self._launch_lock   = asyncio.Lock()

        # [PATCH] Browser storm prevention controls.
        self._last_launch_attempt = 0.0
        self._launch_cooldown_s = float(
            config.get("browser.launch_cooldown_seconds", 5.0)
        )
        self._launch_attempts = deque()
        self._max_launch_attempts_per_minute = int(
            config.get("browser.max_launch_attempts_per_minute", 3)
        )
        self._replenisher_enabled = bool(
            config.get("browser.replenisher_enabled", False)
        )
        self._force_destroy_on_stop = bool(
            config.get("browser.force_destroy_on_stop", True)
        )
        self._destroy_on_release = bool(
            config.get("browser.destroy_on_release", True)
        )
        self._destroy_timeout = float(
            config.get("browser.destroy_timeout", 10.0)
        )
        self._cleanup_orphans_on_start = bool(
            config.get("browser.cleanup_orphans_on_start", True)
        )
        self._cleanup_orphans_on_stop = bool(
            config.get("browser.cleanup_orphans_on_stop", True)
        )
        logger.info(
            f"[BrowserFarm] Initialized (nodriver): "
            f"pool={pool_size}, warmup={warmup_count}"
        )

    # ── Lifecycle ──────────────────────────────────────────

    async def start(self) -> None:
        """Start browser farm and pre-warm pool."""
        if not HAS_NODRIVER:
            raise BrowserLaunchError(
                reason=(
                    "nodriver not installed. "
                    "Run: pip install nodriver"
                ),
            )

        self._running = True

        if self._cleanup_orphans_on_start:
            BrowserInstance.cleanup_orphaned_chrome_processes()

        # Pre-warm
        gui_lazy = bool(
            self._config.get("browser.gui_lazy_startup", False)
            or self._config.get("gui.lazy_browser_startup", False)
        )
        if self._warmup_count > 0 and not gui_lazy:
            logger.info(
                f"[BrowserFarm] Pre-warming "
                f"{self._warmup_count} browsers..."
            )
            tasks = [
                self._launch_browser()
                for _ in range(
                    min(self._warmup_count, self._pool_size)
                )
            ]
            results = await asyncio.gather(
                *tasks, return_exceptions=True
            )
            success = sum(
                1 for r in results
                if not isinstance(r, Exception)
            )
            logger.info(
                f"[BrowserFarm] Pre-warmed: "
                f"{success}/{self._warmup_count}"
            )

        # Start background tasks
        self._health_task = asyncio.create_task(
            self._health_monitor(),
            name="BrowserFarm-Health",
        )
        if self._replenisher_enabled and self._min_available > 0:
            self._warmup_task = asyncio.create_task(
                self._pool_replenisher(),
                name="BrowserFarm-Replenisher",
            )
        else:
            self._warmup_task = None
            logger.info(
                "[BrowserFarm] Pool replenisher disabled; browsers launch only on demand."
            )

        await self._event_bus.publish_simple(
            EventCategory.BROWSER_POOL_WARMED,
            {
                "pool_size": len(self._all),
                "available": self._available.qsize(),
                "engine":    "nodriver",
            },
        )
        logger.info(
            f"[BrowserFarm] Started: "
            f"{len(self._all)} browsers ready"
        )

    async def stop(self, drain_timeout: float = 15.0) -> None:
        """Gracefully stop farm and destroy all browsers."""
        logger.info("[BrowserFarm] Stopping...")
        self._running = False

        for task in [self._health_task, self._warmup_task]:
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        # Wait for in-use browsers only when force-destroy is disabled. In GUI
        # shutdown/emergency stop, force destroy is safer because it prevents
        # background workers from keeping Chrome windows alive.
        if self._in_use and not self._force_destroy_on_stop:
            logger.info(
                f"[BrowserFarm] Draining "
                f"{len(self._in_use)} in-use browsers..."
            )
            drain_start = time.monotonic()
            while self._in_use:
                if (time.monotonic() - drain_start) > drain_timeout:
                    break
                await asyncio.sleep(1.0)
        elif self._in_use:
            logger.warning(
                f"[BrowserFarm] Force destroying {len(self._in_use)} in-use browsers"
            )

        # Destroy all known browsers, then optionally sweep orphaned app-owned
        # Chrome processes that might have survived a crash or PID lookup failure.
        all_browsers = list(self._all.values())
        await asyncio.gather(
            *[b.destroy() for b in all_browsers],
            return_exceptions=True,
        )

        self._all.clear()
        self._in_use.clear()
        self._crashed.clear()

        if self._cleanup_orphans_on_stop:
            BrowserInstance.cleanup_orphaned_chrome_processes()

        logger.info(
            f"[BrowserFarm] Stopped. "
            f"Metrics: {self.metrics.to_dict()}"
        )

    # ── Acquisition ────────────────────────────────────────

    async def acquire(
        self,
        session_id:     str,
        profile:        Optional[BrowserProfile]    = None,
        is_mobile:      bool                        = False,
        timeout:        Optional[float]             = None,
    ) -> BrowserInstance:
        """Acquire a browser instance for a session."""
        timeout = timeout or self._acquisition_timeout
        start   = time.monotonic()

        while self._running:
            browser = await self._try_acquire(session_id, profile)

            if browser:
                wait_ms = (time.monotonic() - start) * 1000
                self.metrics.record_acquisition(wait_ms)
                browser.bind_session(session_id)

                async with self._pool_lock:
                    self._in_use[browser.browser_id] = browser

                await self._event_bus.publish_simple(
                    EventCategory.BROWSER_LAUNCHED,
                    {
                        "browser_id":   browser.browser_id,
                        "session_id":   session_id,
                        "engine":       "nodriver",
                        "pool_size":    len(self._all),
                        "available":    self._available.qsize(),
                        "in_use":       len(self._in_use),
                        "wait_ms":      round(wait_ms, 2),
                    },
                    session_id=session_id,
                )
                return browser

            # Timeout check
            elapsed = time.monotonic() - start
            if elapsed >= timeout:
                raise BrowserPoolExhaustedError(
                    pool_size=self._pool_size,
                    active=len(self._in_use),
                    context=ErrorContext(
                        module="BrowserFarm",
                        operation="acquire",
                        session_id=session_id,
                    ),
                )

            # Launch only one on-demand browser at a time. The rolling launch
            # limiter in _launch_browser() decides whether a new launch is safe.
            if len(self._all) < self._pool_size:
                async with self._launch_lock:
                    if not self._running:
                        break
                    if len(self._all) < self._pool_size:
                        try:
                            browser = await self._launch_browser(profile=profile)
                            if browser:
                                continue
                        except Exception as exc:
                            logger.warning(f"[BrowserFarm] Launch failed: {exc}")

            logger.debug(
                f"[BrowserFarm] Waiting for browser "
                f"(session={session_id[:8]}, "
                f"elapsed={elapsed:.1f}s)"
            )
            await asyncio.sleep(1.0)

        raise BrowserPoolExhaustedError(
            pool_size=self._pool_size,
            active=len(self._in_use),
            context=ErrorContext(
                module="BrowserFarm",
                operation="acquire_stopped",
                session_id=session_id,
            ),
        )

    async def release(
        self,
        browser_id: str,
        recycle:    bool = True,
    ) -> None:
        """Release browser back to pool."""
        async with self._pool_lock:
            browser = self._in_use.pop(browser_id, None)
        if not browser:
            return

        session_id  = browser.unbind_session()
        duration    = browser.uptime
        self.metrics.record_release(duration)

        await self._event_bus.publish_simple(
            EventCategory.BROWSER_RECYCLED,
            {
                "browser_id":   browser_id,
                "session_id":   session_id,
                "pages_loaded": browser.pages_loaded,
                "duration":     round(duration, 2),
            },
        )

        if browser.is_crashed:
            await self._handle_crashed(browser)
            return

        if self._destroy_on_release:
            async with self._pool_lock:
                self._all.pop(browser.browser_id, None)
            try:
                await asyncio.wait_for(
                    browser.destroy(),
                    timeout=max(1.0, self._destroy_timeout),
                )
            except asyncio.TimeoutError:
                logger.warning(
                    f"[BrowserFarm] Destroy timeout, forcing orphan cleanup: {browser_id}"
                )
                try:
                    BrowserInstance.cleanup_orphaned_chrome_processes()
                except Exception:
                    pass
            finally:
                self.metrics.total_destroyed += 1
            logger.debug(
                f"[BrowserFarm] Destroyed after release: {browser_id}"
            )
            return

        if recycle and browser.needs_recycling:
            try:
                await browser.recycle()
                self.metrics.total_recycled += 1
                await self._available.put(browser)
                logger.debug(
                    f"[BrowserFarm] Recycled: {browser_id}"
                )
            except BrowserCrashedError:
                await self._handle_crashed(browser)
        elif not browser.is_crashed:
            await self._available.put(browser)

    @asynccontextmanager
    async def browser_context(
        self,
        session_id: str,
        profile:    Optional[BrowserProfile]    = None,
        is_mobile:  bool                        = False,
    ):
        """Async context manager for browser lifecycle."""
        browser = await self.acquire(
            session_id=session_id,
            profile=profile,
            is_mobile=is_mobile,
        )
        crashed = False
        try:
            yield browser
        except BrowserCrashedError:
            crashed = True
            raise
        except Exception:
            raise
        finally:
            await self.release(
                browser_id=browser.browser_id,
                recycle=not crashed,
            )

    # ── Status ─────────────────────────────────────────────

    @property
    def available_count(self) -> int:
        return self._available.qsize()

    @property
    def in_use_count(self) -> int:
        return len(self._in_use)

    @property
    def total_count(self) -> int:
        return len(self._all)

    @property
    def crashed_count(self) -> int:
        return len(self._crashed)

    def get_status(self) -> Dict[str, Any]:
        total_memory = sum(
            b._memory_mb for b in self._all.values()
        )
        return {
            "engine":           "nodriver",
            "total":            self.total_count,
            "available":        self.available_count,
            "in_use":           self.in_use_count,
            "crashed":          self.crashed_count,
            "pool_size":        self._pool_size,
            "total_memory_mb":  round(total_memory, 1),
            "max_memory_mb":    self._max_memory_mb,
            "metrics":          self.metrics.to_dict(),
        }

    # ── Internal ───────────────────────────────────────────

    async def _try_acquire(
        self,
        session_id: str,
        profile:    Optional[BrowserProfile],
    ) -> Optional[BrowserInstance]:
        """Try to get browser from pool."""
        try:
            browser = self._available.get_nowait()

            # Health check
            if browser.is_crashed:
                async with self._pool_lock:
                    self._all.pop(browser.browser_id, None)
                await browser.destroy()
                self.metrics.total_destroyed += 1
                return None

            # Verify browser is alive via nodriver
            if not await self._is_alive(browser):
                async with self._pool_lock:
                    self._all.pop(browser.browser_id, None)
                await browser.destroy()
                self.metrics.total_destroyed += 1
                return None

            return browser

        except asyncio.QueueEmpty:
            return None

    async def _is_alive(self, browser: BrowserInstance) -> bool:
        """Check if the nodriver browser object is usable.

        A freshly launched headless Chrome can reject a quick JS probe on
        about:blank while CDP/proxy-auth handlers are still settling. The older
        probe destroyed healthy browsers immediately after launch, which caused
        repeated launch/terminate loops and BrowserPoolExhaustedError. For this
        on-demand pool, a non-crashed BrowserInstance with a browser object and
        page handle is considered alive; navigation will perform the real
        verification later and record success/failure in the session report.
        """
        try:
            if browser is None or browser.is_crashed:
                return False
            if browser._browser is None or browser._page is None:
                return False
            return True
        except Exception:
            return False

    async def _launch_browser(
        self,
        profile: Optional[BrowserProfile] = None,
    ) -> Optional[BrowserInstance]:
        """Launch a new nodriver browser instance."""
        async with self._launch_sem:
            if not self._running:
                return None

            # Browser storm guard: one launch cooldown + rolling 60s limit.
            now = time.time()
            while self._launch_attempts and now - self._launch_attempts[0] > 60:
                self._launch_attempts.popleft()

            if len(self._launch_attempts) >= self._max_launch_attempts_per_minute:
                logger.warning(
                    "[BrowserFarm] Launch throttled: "
                    f"{len(self._launch_attempts)} attempts in the last 60s"
                )
                return None

            time_since_last = now - self._last_launch_attempt
            if time_since_last < self._launch_cooldown_s:
                await asyncio.sleep(self._launch_cooldown_s - time_since_last)

            self._last_launch_attempt = time.time()
            self._launch_attempts.append(self._last_launch_attempt)

            if len(self._all) >= self._pool_size:
                return None

            # Memory check
            total_mem = sum(
                b._memory_mb for b in self._all.values()
            )
            if total_mem >= self._max_memory_mb:
                logger.warning(
                    f"[BrowserFarm] Memory limit: "
                    f"{total_mem:.0f}MB / {self._max_memory_mb:.0f}MB"
                )
                return None

            # Build profile
            if profile is None:
                if self._profile_factory:
                    profile = self._profile_factory()
                else:
                    profile = self._default_profile()

            browser_id  = str(uuid.uuid4())[:12]
            browser     = BrowserInstance(
                browser_id      = browser_id,
                profile         = profile,
                config          = self._config,
                recycle_after   = self._recycle_after,
            )

            try:
                await browser.launch()
                async with self._pool_lock:
                    self._all[browser_id] = browser
                await self._available.put(browser)
                self.metrics.total_launched += 1

                logger.debug(
                    f"[BrowserFarm] Launched (nodriver): "
                    f"{browser_id} | "
                    f"pool: {len(self._all)}/{self._pool_size}"
                )
                return browser

            except BrowserLaunchError as exc:
                self.metrics.total_warmup_failures += 1
                logger.error(
                    f"[BrowserFarm] Launch failed: {exc}"
                )
                await browser.destroy()
                return None

    def _default_profile(self) -> BrowserProfile:
        """Create default browser profile."""
        user_agents = [
            (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.6422.142 Safari/537.36"
            ),
            (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.6422.142 Safari/537.36"
            ),
            (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.6367.82 Safari/537.36"
            ),
        ]
        return BrowserProfile(
            profile_id      = str(uuid.uuid4())[:10],
            user_agent      = random.choice(user_agents),
            viewport_width  = self._config.get(
                "browser.window_width", 1920
            ),
            viewport_height = self._config.get(
                "browser.window_height", 1080
            ),
            headless        = self._config.get(
                "browser.headless", True
            ),
            disable_images  = self._config.get(
                "browser.disable_images", False
            ),
        )

    async def _handle_crashed(
        self,
        browser: BrowserInstance,
    ) -> None:
        """Handle a crashed browser."""
        self.metrics.total_crashed += 1
        browser_id = browser.browser_id

        async with self._pool_lock:
            self._all.pop(browser_id, None)
            self._crashed[browser_id] = browser

        await self._event_bus.publish_simple(
            EventCategory.BROWSER_CRASHED,
            {
                "browser_id":   browser_id,
                "pages_loaded": browser.pages_loaded,
                "uptime":       round(browser.uptime, 1),
            },
            priority=EventPriority.HIGH,
        )

        # Destroy
        try:
            await browser.destroy()
        except Exception:
            pass

        async with self._pool_lock:
            self._crashed.pop(browser_id, None)

        self.metrics.total_destroyed += 1

        # Recovery
        if self._crash_recovery and self._running:
            logger.info(
                f"[BrowserFarm] Recovering: {browser_id}"
            )
            try:
                await self._launch_browser()
            except Exception as exc:
                logger.error(
                    f"[BrowserFarm] Recovery failed: {exc}"
                )

    # ── Background Tasks ───────────────────────────────────

    async def _health_monitor(self) -> None:
        """Monitor browser health."""
        while self._running:
            try:
                await asyncio.sleep(self._health_check_interval)

                browsers        = list(self._all.values())
                total_memory    = 0.0

                for browser in browsers:
                    if browser.is_crashed:
                        if browser.browser_id not in self._in_use:
                            await self._handle_crashed(browser)
                        continue

                    # Get resource usage
                    usage = await browser.get_resource_usage()
                    total_memory += usage.get("memory_mb", 0)

                    # Check if browser process is alive
                    if browser.browser_id not in self._in_use:
                        alive = await self._is_alive(browser)
                        if not alive:
                            logger.warning(
                                f"[BrowserFarm] Dead browser: "
                                f"{browser.browser_id}"
                            )
                            await self._handle_crashed(browser)

                self.metrics.record_memory(total_memory)

                await self._event_bus.publish_simple(
                    EventCategory.HEALTH_CHECK,
                    {
                        "component":    "browser_farm",
                        "engine":       "nodriver",
                        "total":        len(browsers),
                        "available":    self._available.qsize(),
                        "in_use":       len(self._in_use),
                        "memory_mb":    round(total_memory, 1),
                    },
                    priority=EventPriority.LOW,
                )

            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error(
                    f"[BrowserFarm] Health monitor error: {exc}"
                )

    async def _pool_replenisher(self) -> None:
        """Replenish pool to maintain minimum available."""
        if not self._replenisher_enabled or self._min_available <= 0:
            logger.info("[BrowserFarm] Replenisher exited: disabled by config")
            return
        while self._running:
            try:
                await asyncio.sleep(5.0)

                available   = self._available.qsize()
                total       = len(self._all)

                if (
                    available < self._min_available and
                    total < self._pool_size
                ):
                    needed = min(
                        self._min_available - available,
                        self._pool_size - total,
                    )
                    logger.debug(
                        f"[BrowserFarm] Replenishing "
                        f"{needed} browsers"
                    )
                    for _ in range(needed):
                        if not self._running:
                            break
                        try:
                            await self._launch_browser()
                        except Exception as exc:
                            logger.warning(
                                f"[BrowserFarm] Replenish error: {exc}"
                            )
                            break

            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error(
                    f"[BrowserFarm] Replenisher error: {exc}"
                )