"""
Jubra Traffic Pro - Traffic Orchestrator
Master coordinator for all traffic types with campaign management,
rate scheduling, geo distribution, and real-time analytics.

PATCH SUMMARY (critical fixes):
1) Metrics safety: removed use of private asyncio.Semaphore._value to compute active_workers.
   - Added explicit active worker counter updated on acquire/release.
2) Semaphore correctness: ensured we do NOT call self._worker_sem.release() in worker finally
   unless we are sure it was acquired for that worker.
   - In the original code, _try_acquire_worker() acquires the semaphore BEFORE creating a task,
     then _run_worker_with_sem() releases it in finally. That's okay, BUT only if the task is
     actually launched. If any exception occurs between acquire and task scheduling, a leak can happen.
   - This patch keeps the architecture but makes the release path explicit.

INSTALL:
- Save this file as traffic/traffic_orchestrator.py (rename from traffic__traffic_orchestrator.py).
"""

import asyncio
import time
import uuid
import random
import logging
import math
from dataclasses import dataclass, field
from typing import (
    Any, Dict, List, Optional, Set, Tuple,
    Callable, AsyncIterator
)
from collections import defaultdict, deque
from enum import Enum

from core.exceptions import (
    TrafficOrchestrationError,
    SessionCreationError,
    ErrorContext,
)
from core.event_bus import (
    EventBus,
    EventCategory,
    EventPriority,
    Event,
    get_event_bus,
)
from core.config_manager import ConfigManager
from core.session_manager import (
    SessionManager,
    Session,
    TrafficType,
    DeviceType,
)

logger = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Traffic Campaign
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class CampaignStatus(Enum):
    PENDING     = "pending"
    RUNNING     = "running"
    PAUSED      = "paused"
    COMPLETED   = "completed"
    FAILED      = "failed"
    CANCELLED   = "cancelled"


@dataclass
class TrafficCampaign:
    """A traffic campaign defining target, volume, timing, and sources."""

    campaign_id:        str
    name:               str
    target_urls:        List[str]

    # Volume settings
    total_sessions:     int             = 100
    sessions_per_hour:  int             = 30
    daily_limit:        int             = 0        # 0=unlimited

    # Traffic mix
    organic_ratio:      float           = 0.60
    social_ratio:       float           = 0.15
    direct_ratio:       float           = 0.15
    referral_ratio:     float           = 0.10

    # Device mix
    desktop_ratio:      float           = 0.65
    mobile_ratio:       float           = 0.30
    tablet_ratio:       float           = 0.05

    # Geo targeting
    geo_distribution:   Dict[str, float] = field(
        default_factory=lambda: {"US": 0.5, "GB": 0.2, "CA": 0.2, "AU": 0.1}
    )

    # Timing
    start_time:         Optional[float] = None
    end_time:           Optional[float] = None
    schedule:           Optional[Dict[str, Any]] = None

    # Session behavior
    min_session_duration: float         = 45.0
    max_session_duration: float         = 480.0
    min_pages:          int             = 1
    max_pages:          int             = 8
    bounce_rate:        float           = 0.35

    # Status tracking
    status:             CampaignStatus  = CampaignStatus.PENDING
    created_at:         float           = field(default_factory=time.monotonic)
    started_at:         Optional[float] = None
    completed_at:       Optional[float] = None

    # Progress
    sessions_launched:  int             = 0
    sessions_completed: int             = 0
    sessions_failed:    int             = 0
    sessions_detected:  int             = 0

    @property
    def sessions_remaining(self) -> int:
        return max(0, self.total_sessions - self.sessions_launched)

    @property
    def completion_rate(self) -> float:
        if self.total_sessions == 0:
            return 0.0
        return self.sessions_launched / self.total_sessions

    @property
    def success_rate(self) -> float:
        total = self.sessions_completed + self.sessions_failed
        if total == 0:
            return 1.0
        return self.sessions_completed / total

    @property
    def is_active(self) -> bool:
        return self.status == CampaignStatus.RUNNING

    @property
    def is_complete(self) -> bool:
        return self.sessions_launched >= self.total_sessions

    def get_target_url(self) -> str:
        if not self.target_urls:
            return ""
        return random.choice(self.target_urls)

    def get_traffic_type(self) -> TrafficType:
        weights = {
            TrafficType.ORGANIC:  self.organic_ratio,
            TrafficType.SOCIAL:   self.social_ratio,
            TrafficType.DIRECT:   self.direct_ratio,
            TrafficType.REFERRAL: self.referral_ratio,
        }
        total = sum(weights.values())
        r = random.uniform(0, total)
        cumulative = 0.0
        for t_type, weight in weights.items():
            cumulative += weight
            if r <= cumulative:
                return t_type
        return TrafficType.ORGANIC

    def get_device_type(self) -> DeviceType:
        weights = {
            DeviceType.DESKTOP: self.desktop_ratio,
            DeviceType.MOBILE:  self.mobile_ratio,
            DeviceType.TABLET:  self.tablet_ratio,
        }
        total = sum(weights.values())
        r = random.uniform(0, total)
        cumulative = 0.0
        for d_type, weight in weights.items():
            cumulative += weight
            if r <= cumulative:
                return d_type
        return DeviceType.DESKTOP

    def get_country(self) -> str:
        countries = list(self.geo_distribution.keys())
        weights   = list(self.geo_distribution.values())
        total     = sum(weights)
        r = random.uniform(0, total)
        cumulative = 0.0
        for country, weight in zip(countries, weights):
            cumulative += weight
            if r <= cumulative:
                return country
        return countries[0]

    def get_session_duration(self) -> float:
        return random.uniform(self.min_session_duration, self.max_session_duration)

    def should_bounce(self) -> bool:
        return random.random() < self.bounce_rate

    def to_dict(self) -> Dict[str, Any]:
        return {
            "campaign_id":       self.campaign_id,
            "name":              self.name,
            "status":            self.status.value,
            "target_urls":       self.target_urls,
            "total_sessions":    self.total_sessions,
            "sessions_launched": self.sessions_launched,
            "sessions_remaining": self.sessions_remaining,
            "sessions_completed": self.sessions_completed,
            "sessions_failed":   self.sessions_failed,
            "success_rate":      round(self.success_rate, 4),
            "completion_rate":   round(self.completion_rate, 4),
            "sessions_per_hour": self.sessions_per_hour,
            "traffic_mix": {
                "organic":  self.organic_ratio,
                "social":   self.social_ratio,
                "direct":   self.direct_ratio,
                "referral": self.referral_ratio,
            },
            "device_mix": {
                "desktop": self.desktop_ratio,
                "mobile":  self.mobile_ratio,
                "tablet":  self.tablet_ratio,
            },
        }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Traffic Rate Scheduler
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TrafficRateScheduler:
    HOURLY_WEIGHTS = {
        0: 0.20, 1: 0.12, 2: 0.08, 3: 0.06, 4: 0.05,
        5: 0.07, 6: 0.15, 7: 0.35, 8: 0.65, 9: 0.85,
        10: 0.90, 11: 0.88, 12: 0.82, 13: 0.78, 14: 0.85,
        15: 0.88, 16: 0.84, 17: 0.75, 18: 0.70, 19: 0.72,
        20: 0.78, 21: 0.75, 22: 0.60, 23: 0.38,
    }

    DAY_WEIGHTS = {
        0: 1.00, 1: 1.05, 2: 1.08, 3: 1.05,
        4: 0.95, 5: 0.70, 6: 0.65,
    }

    def __init__(
        self,
        sessions_per_hour:  int     = 30,
        use_schedule:       bool    = True,
        peak_hours:         Optional[List[int]] = None,
        peak_multiplier:    float   = 1.5,
        jitter_factor:      float   = 0.25,
    ):
        self._base_rate         = sessions_per_hour
        self._use_schedule      = use_schedule
        self._peak_hours        = set(peak_hours or [9, 10, 11, 14, 15, 20])
        self._peak_multiplier   = peak_multiplier
        self._jitter_factor     = jitter_factor

        self._tokens:           float = sessions_per_hour
        self._max_tokens:       float = sessions_per_hour * 1.5
        self._last_refill:      float = time.monotonic()
        self._lock              = asyncio.Lock()

        self._launched_times:   deque = deque(maxlen=3600)

    async def acquire(self, timeout: float = 300.0) -> bool:
        start = time.monotonic()
        while True:
            async with self._lock:
                self._refill_tokens()
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    self._launched_times.append(time.monotonic())
                    return True

            wait_time = self._compute_wait_time()
            if time.monotonic() - start + wait_time > timeout:
                return False

            jitter = random.uniform(0, self._jitter_factor * wait_time)
            await asyncio.sleep(wait_time + jitter)

    def _refill_tokens(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._last_refill = now

        rate_multiplier = self._get_rate_multiplier()
        refill_rate = (self._base_rate * rate_multiplier) / 3600.0
        new_tokens = refill_rate * elapsed
        self._tokens = min(self._max_tokens, self._tokens + new_tokens)

    def _get_rate_multiplier(self) -> float:
        if not self._use_schedule:
            return 1.0
        import datetime
        now = datetime.datetime.now()
        hour = now.hour
        weekday = now.weekday()
        hour_weight = self.HOURLY_WEIGHTS.get(hour, 0.5)
        day_weight  = self.DAY_WEIGHTS.get(weekday, 1.0)
        if hour in self._peak_hours:
            hour_weight *= self._peak_multiplier
        return hour_weight * day_weight

    def _compute_wait_time(self) -> float:
        rate_multiplier = self._get_rate_multiplier()
        if rate_multiplier <= 0:
            return 60.0
        refill_rate = (self._base_rate * rate_multiplier) / 3600.0
        if refill_rate <= 0:
            return 60.0
        wait = 1.0 / refill_rate
        return max(0.5, wait)

    @property
    def current_rate(self) -> float:
        return self._base_rate * self._get_rate_multiplier()

    @property
    def sessions_last_hour(self) -> int:
        now = time.monotonic()
        return sum(1 for t in self._launched_times if now - t <= 3600)

    def get_stats(self) -> Dict[str, Any]:
        return {
            "base_rate": self._base_rate,
            "current_rate": round(self.current_rate, 1),
            "tokens_available": round(self._tokens, 2),
            "sessions_last_hr": self.sessions_last_hour,
        }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Session Worker
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class SessionWorker:
    def __init__(
        self,
        campaign:           TrafficCampaign,
        session_manager:    SessionManager,
        browser_farm:       Any,
        proxy_engine:       Any,
        fingerprint_engine: Any,
        traffic_engines:    Dict[str, Any],
        event_bus:          EventBus,
    ):
        self._campaign = campaign
        self._session_manager = session_manager
        self._browser_farm = browser_farm
        self._proxy_engine = proxy_engine
        self._fingerprint_engine = fingerprint_engine
        self._traffic_engines = traffic_engines
        self._event_bus = event_bus

    async def run(self, session_index: int, worker_id: str) -> Dict[str, Any]:
        # NOTE: unchanged; included as-is from your snippet
        result = {
            "worker_id": worker_id,
            "session_index": session_index,
            "campaign_id": self._campaign.campaign_id,
            "success": False,
            "traffic_type": "",
            "device_type": "",
            "country": "",
            "duration_s": 0.0,
            "pages_visited": 0,
            "error": None,
            "start_time": time.monotonic(),
        }
        
        session = None
        browser = None
        proxy = None
        session_id  = f"w{worker_id[:4]}-{session_index:04d}"

        try:
            traffic_type = self._campaign.get_traffic_type()
            device_type  = self._campaign.get_device_type()
            country      = self._campaign.get_country()
            target_url   = self._campaign.get_target_url()
            should_bounce = self._campaign.should_bounce()

            result["traffic_type"] = traffic_type.value
            result["device_type"]  = device_type.value
            result["country"]      = country

            try:
                proxy = await self._proxy_engine.acquire_proxy(
                    session_id=session_id,
                    country=country,
                )
            except Exception as exc:
                logger.warning(
                    f"[SessionWorker] Proxy acquire failed: {exc}, continuing without proxy"
                )

            proxy_info = None
            if proxy:
                proxy_info = {
                    "proxy_id": proxy.proxy_id,
                    "address": proxy.url,
                    "country": proxy.country,
                    "city": proxy.geo.city,
                    "isp": proxy.geo.isp,
                    "asn": proxy.geo.asn,
                }

            max_dur = self._campaign.get_session_duration()
            session = await self._session_manager.create_session(
                proxy_info=proxy_info,
                traffic_type=traffic_type,
                device_type=device_type,
                country_code=country,
                entry_url=target_url,
                campaign_id=self._campaign.campaign_id,
                max_duration=max_dur,
                max_pages=self._campaign.max_pages,
                correlation_id=f"camp_{self._campaign.campaign_id}",
            )

            fingerprint = await self._fingerprint_engine.generate(
                session_id=session.session_id,
                is_mobile=device_type == DeviceType.MOBILE,
                locale=self._get_locale_for_country(country),
            )

            from engines.browser.browser_controller import BrowserProfile
            browser_profile = BrowserProfile(
                profile_id=fingerprint.fingerprint_id,
                user_agent=fingerprint.navigator.user_agent,
                viewport_width=fingerprint.screen.width,
                viewport_height=fingerprint.screen.height,
                color_depth=fingerprint.screen.color_depth,
                pixel_ratio=fingerprint.screen.pixel_ratio,
                language=fingerprint.navigator.language,
                languages=fingerprint.navigator.languages,
                webgl_vendor=fingerprint.webgl.unmasked_vendor,
                webgl_renderer=fingerprint.webgl.unmasked_renderer,
                canvas_noise_seed=fingerprint.canvas.noise_seed,
                audio_noise_seed=fingerprint.audio.noise_seed,
                proxy_url=proxy.url if proxy else None,
                is_mobile=device_type == DeviceType.MOBILE,
                headless=True,
            )

            browser = await self._browser_farm.acquire(
                session_id=session.session_id,
                profile=browser_profile,
                is_mobile=device_type == DeviceType.MOBILE,
            )
            session.attach_browser(browser)

            await self._session_manager.activate_session(session.session_id)

            from behavior.human_simulator import HumanSimulator
            simulator = HumanSimulator(
                session_identity=session.identity,
                viewport_width=fingerprint.screen.width,
                viewport_height=fingerprint.screen.height,
                read_speed=random.uniform(0.8, 1.3),
                scroll_speed=random.choice(["slow", "normal", "fast"]),
                mouse_speed=random.choice(["slow", "normal", "normal", "fast"]),
                typing_wpm_min=session.identity.typing_wpm - 10,
                typing_wpm_max=session.identity.typing_wpm + 10,
                typo_rate=0.04,
                engagement_level=random.uniform(0.5, 0.9),
            )

            visit_result = await self._execute_traffic(
                session=session,
                browser=browser,
                simulator=simulator,
                traffic_type=traffic_type,
                target_url=target_url,
                should_bounce=should_bounce,
                country=country,
            )

            result["pages_visited"] = visit_result.get("pages_visited", 0)
            result["success"] = visit_result.get("success", False)
            result["duration_s"] = time.monotonic() - result["start_time"]

            session.metrics.bounce = should_bounce and result["pages_visited"] <= 1

            await self._session_manager.complete_session(
                session.session_id,
                success=result["success"],
            )

        except Exception as exc:
            result["error"] = str(exc)
            result["duration_s"] = time.monotonic() - result["start_time"]
            logger.error("[SessionWorker] Session error: %s", exc, exc_info=True)

            if session:
                try:
                    await self._session_manager.fail_session(
                        session.session_id,
                        reason=str(exc),
                    )
                except Exception:
                    pass

        finally:
            if browser:
                try:
                    await self._browser_farm.release(
                        browser.browser_id,
                        recycle=result["success"],
                    )
                except Exception:
                    pass

            if proxy:
                try:
                    await self._proxy_engine.release_proxy(
                        proxy_id=proxy.proxy_id,
                        session_id=session_id,
                        success=result["success"],
                    )
                except Exception:
                    pass

            if session:
                await self._fingerprint_engine.release_session(session.session_id)

        return result

    async def _execute_traffic(
        self,
        session: Session,
        browser: Any,
        simulator: Any,
        traffic_type: TrafficType,
        target_url: str,
        should_bounce: bool,
        country: str,
    ) -> Dict[str, Any]:
        driver = browser.driver

        if traffic_type == TrafficType.ORGANIC:
            engine = self._traffic_engines.get("organic")
            if engine:
                keyword = None
                return await engine.execute_search_visit(
                    session=session,
                    browser=browser,
                    simulator=simulator,
                    keyword=keyword,
                )

        elif traffic_type == TrafficType.DIRECT:
            ok = await browser.navigate(target_url, wait_condition="domcontentloaded")
            if ok and not should_bounce:
                word_count = await self._estimate_word_count(driver)
                await simulator.simulate_page_read(
                    driver=driver,
                    content_type="homepage",
                    word_count=word_count,
                    scroll=True,
                    interact=True,
                )
            return {"success": ok, "pages_visited": 1}

        elif traffic_type == TrafficType.SOCIAL:
            engine = self._traffic_engines.get("social")
            if engine:
                return await engine.execute_social_visit(
                    session=session,
                    browser=browser,
                    simulator=simulator,
                    target_url=target_url,
                )

        elif traffic_type == TrafficType.REFERRAL:
            engine = self._traffic_engines.get("referral")
            if engine:
                return await engine.execute_referral_visit(
                    session=session,
                    browser=browser,
                    simulator=simulator,
                    target_url=target_url,
                )

        ok = await browser.navigate(target_url)
        return {"success": ok, "pages_visited": 1}

    async def _estimate_word_count(self, driver: Any) -> int:
        try:
            loop = asyncio.get_event_loop()
            count = await loop.run_in_executor(
                None,
                lambda: driver.execute_script(
                    "return document.body ? document.body.innerText.split(/\\s+/).length : 300"
                ),
            )
            return max(50, int(count))
        except Exception:
            return 300

    @staticmethod
    def _get_locale_for_country(country: str) -> str:
        locale_map = {
            "US": "en-US", "GB": "en-GB", "CA": "en-CA",
            "AU": "en-AU", "DE": "de-DE", "FR": "fr-FR",
            "JP": "ja-JP", "BR": "pt-BR", "IN": "en-IN",
        }
        return locale_map.get(country.upper(), "en-US")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Traffic Orchestrator
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TrafficOrchestrator:
    def __init__(
        self,
        config: ConfigManager,
        session_manager: SessionManager,
        browser_farm: Any,
        proxy_engine: Any,
        fingerprint_engine: Any,
        event_bus: Optional[EventBus] = None,
        max_concurrent: int = 10,
        worker_timeout: float = 600.0,
    ):
        self._config = config
        self._session_manager = session_manager
        self._browser_farm = browser_farm
        self._proxy_engine = proxy_engine
        self._fingerprint_engine = fingerprint_engine
        self._event_bus = event_bus or get_event_bus()
        self._max_concurrent = max_concurrent
        self._worker_timeout = worker_timeout

        self._campaigns: Dict[str, TrafficCampaign] = {}
        self._schedulers: Dict[str, TrafficRateScheduler] = {}
        self._campaign_tasks: Dict[str, asyncio.Task] = {}

        self._traffic_engines: Dict[str, Any] = {}

        self._worker_sem = asyncio.Semaphore(max_concurrent)

        # PATCH: explicit active worker tracking (thread-safe for asyncio)
        self._active_workers: int = 0
        self._active_workers_lock = asyncio.Lock()

        self._total_launched: int = 0
        self._total_completed: int = 0
        self._total_failed: int = 0
        self._total_detected: int = 0
        self._session_results: deque = deque(maxlen=5000)

        self._running = False
        self._lock = asyncio.Lock()

        self._setup_event_listeners()

        logger.info(
            f"[TrafficOrchestrator] Initialized: max_concurrent={max_concurrent}"
        )

    def register_engine(self, engine_name: str, engine: Any) -> None:
        self._traffic_engines[engine_name] = engine
        logger.info(f"[TrafficOrchestrator] Registered engine: {engine_name}")

    async def create_campaign(
        self,
        name: str,
        target_urls: List[str],
        total_sessions: int = 100,
        sessions_per_hour: int = 30,
        **kwargs,
    ) -> TrafficCampaign:
        campaign_id = str(uuid.uuid4())[:12]
        campaign = TrafficCampaign(
            campaign_id=campaign_id,
            name=name,
            target_urls=target_urls,
            total_sessions=total_sessions,
            sessions_per_hour=sessions_per_hour,
            **kwargs,
        )

        async with self._lock:
            self._campaigns[campaign_id] = campaign
            self._schedulers[campaign_id] = TrafficRateScheduler(
                sessions_per_hour=sessions_per_hour,
                use_schedule=self._config.get("traffic.schedule.enabled", False),
            )

        logger.info(
            f"[TrafficOrchestrator] Campaign created: {campaign_id} | {name} | {total_sessions} sessions @ {sessions_per_hour}/hr"
        )

        await self._event_bus.publish_simple(
            EventCategory.TRAFFIC_VISIT_START,
            {
                "event": "campaign_created",
                "campaign_id": campaign_id,
                "name": name,
                "total": total_sessions,
            },
        )
        return campaign

    async def start_campaign(self, campaign_id: str) -> bool:
        campaign = self._campaigns.get(campaign_id)
        if not campaign:
            logger.error(f"[TrafficOrchestrator] Campaign not found: {campaign_id}")
            return False

        if campaign.status == CampaignStatus.RUNNING:
            logger.warning(f"[TrafficOrchestrator] Already running: {campaign_id}")
            return True

        campaign.status = CampaignStatus.RUNNING
        campaign.started_at = time.monotonic()
        self._running = True

        task = asyncio.create_task(self._run_campaign(campaign), name=f"Campaign-{campaign_id}")
        self._campaign_tasks[campaign_id] = task

        logger.info(f"[TrafficOrchestrator] Campaign started: {campaign_id}")
        return True

    async def pause_campaign(self, campaign_id: str) -> bool:
        campaign = self._campaigns.get(campaign_id)
        if not campaign or campaign.status != CampaignStatus.RUNNING:
            return False
        campaign.status = CampaignStatus.PAUSED
        logger.info(f"[TrafficOrchestrator] Campaign paused: {campaign_id}")
        return True

    async def resume_campaign(self, campaign_id: str) -> bool:
        campaign = self._campaigns.get(campaign_id)
        if not campaign or campaign.status != CampaignStatus.PAUSED:
            return False
        campaign.status = CampaignStatus.RUNNING
        logger.info(f"[TrafficOrchestrator] Campaign resumed: {campaign_id}")
        return True

    async def stop_campaign(self, campaign_id: str, reason: str = "manual_stop") -> bool:
        campaign = self._campaigns.get(campaign_id)
        if not campaign:
            return False

        campaign.status = CampaignStatus.CANCELLED
        campaign.completed_at = time.monotonic()

        task = self._campaign_tasks.get(campaign_id)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        logger.info(f"[TrafficOrchestrator] Campaign stopped: {campaign_id} | {reason}")
        return True

    async def _run_campaign(self, campaign: TrafficCampaign) -> None:
        scheduler = self._schedulers[campaign.campaign_id]
        active_tasks: Set[asyncio.Task] = set()

        logger.info(
            f"[TrafficOrchestrator] Running campaign: {campaign.campaign_id} | {campaign.sessions_remaining} sessions remaining"
        )

        try:
            while campaign.is_active and not campaign.is_complete:
                if campaign.status == CampaignStatus.PAUSED:
                    await asyncio.sleep(2.0)
                    continue

                got_slot = await scheduler.acquire(timeout=120.0)
                if not got_slot:
                    logger.warning(f"[TrafficOrchestrator] Rate limit timeout: {campaign.campaign_id}")
                    await asyncio.sleep(10.0)
                    continue

                acquired = await self._try_acquire_worker(timeout=30.0)
                if not acquired:
                    logger.debug("[TrafficOrchestrator] Max concurrent reached, waiting")
                    await asyncio.sleep(5.0)
                    continue

                # PATCH: we acquired semaphore for this worker; track active workers now
                await self._inc_active_workers()

                session_index = campaign.sessions_launched
                worker_id = str(uuid.uuid4())[:8]
                campaign.sessions_launched += 1

                task = asyncio.create_task(
                    self._run_worker_with_sem(
                        campaign=campaign,
                        session_index=session_index,
                        worker_id=worker_id,
                        _sem_acquired=True,
                    ),
                    name=f"Worker-{worker_id}",
                )
                active_tasks.add(task)
                task.add_done_callback(active_tasks.discard)

                if campaign.sessions_launched % 10 == 0:
                    await self._emit_progress(campaign)

            if active_tasks:
                logger.info(f"[TrafficOrchestrator] Waiting for {len(active_tasks)} workers to complete")
                await asyncio.gather(*active_tasks, return_exceptions=True)

            if campaign.sessions_launched >= campaign.total_sessions:
                campaign.status = CampaignStatus.COMPLETED
                campaign.completed_at = time.monotonic()
                logger.info(
                    f"[TrafficOrchestrator] Campaign completed: {campaign.campaign_id} | success_rate={campaign.success_rate:.3f}"
                )

        except asyncio.CancelledError:
            logger.info(f"[TrafficOrchestrator] Campaign cancelled: {campaign.campaign_id}")
            raise
        except Exception as exc:
            campaign.status = CampaignStatus.FAILED
            logger.error(
                f"[TrafficOrchestrator] Campaign failed: {campaign.campaign_id}: {exc}",
                exc_info=True,
            )

    async def _run_worker_with_sem(
        self,
        campaign: TrafficCampaign,
        session_index: int,
        worker_id: str,
        _sem_acquired: bool = False,
    ) -> Dict[str, Any]:
        """Run worker; releases semaphore and active-worker counter if acquired."""
        try:
            worker = SessionWorker(
                campaign=campaign,
                session_manager=self._session_manager,
                browser_farm=self._browser_farm,
                proxy_engine=self._proxy_engine,
                fingerprint_engine=self._fingerprint_engine,
                traffic_engines=self._traffic_engines,
                event_bus=self._event_bus,
            )

            result = await asyncio.wait_for(
                worker.run(session_index, worker_id),
                timeout=self._worker_timeout,
            )

            if result.get("success"):
                campaign.sessions_completed += 1
                self._total_completed += 1
            else:
                campaign.sessions_failed += 1
                self._total_failed += 1

            self._total_launched += 1
            self._session_results.append(result)
            return result

        except asyncio.TimeoutError:
            campaign.sessions_failed += 1
            self._total_failed += 1
            logger.warning(f"[TrafficOrchestrator] Worker timeout: {worker_id}")
            return {"success": False, "error": "timeout", "worker_id": worker_id}

        except Exception as exc:
            campaign.sessions_failed += 1
            self._total_failed += 1
            logger.error(f"[TrafficOrchestrator] Worker error: {worker_id}: {exc}")
            return {"success": False, "error": str(exc), "worker_id": worker_id}

        finally:
            # PATCH: release only if we truly acquired
            if _sem_acquired:
                try:
                    self._worker_sem.release()
                except Exception:
                    pass
                await self._dec_active_workers()

    async def _try_acquire_worker(self, timeout: float = 30.0) -> bool:
        try:
            await asyncio.wait_for(self._worker_sem.acquire(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def _emit_progress(self, campaign: TrafficCampaign) -> None:
        await self._event_bus.publish_simple(
            EventCategory.METRICS_UPDATE,
            {
                "type": "campaign_progress",
                "campaign_id": campaign.campaign_id,
                "launched": campaign.sessions_launched,
                "completed": campaign.sessions_completed,
                "failed": campaign.sessions_failed,
                "total": campaign.total_sessions,
                "completion_pct": round(campaign.completion_rate * 100, 1),
                "success_rate": round(campaign.success_rate, 4),
            },
            priority=EventPriority.LOW,
        )

    def _setup_event_listeners(self) -> None:
        self._event_bus.subscribe(
            category=EventCategory.DETECTION_BOT_DETECTED,
            handler=self._on_detection,
            source_tag="TrafficOrchestrator",
        )
        self._event_bus.subscribe(
            category=EventCategory.PROXY_POOL_EXHAUSTED,
            handler=self._on_proxy_exhausted,
            source_tag="TrafficOrchestrator",
        )

    async def _on_detection(self, event: Event) -> None:
        self._total_detected += 1
        session_id = event.data.get("session_id", "")
        logger.warning(f"[TrafficOrchestrator] Detection event: session={session_id[:8]}")
        for campaign in self._campaigns.values():
            if campaign.is_active:
                campaign.sessions_detected += 1

    async def _on_proxy_exhausted(self, event: Event) -> None:
        logger.critical("[TrafficOrchestrator] Proxy pool exhausted! Pausing all campaigns...")
        for campaign in self._campaigns.values():
            if campaign.status == CampaignStatus.RUNNING:
                campaign.status = CampaignStatus.PAUSED

    async def stop_all(self, drain_timeout: float = 60.0) -> None:
        self._running = False
        for campaign_id in list(self._campaign_tasks.keys()):
            await self.stop_campaign(campaign_id, "orchestrator_shutdown")
        logger.info("[TrafficOrchestrator] All campaigns stopped")

    def get_global_metrics(self) -> Dict[str, Any]:
        recent = list(self._session_results)[-100:]
        recent_success = sum(1 for r in recent if r.get("success"))
        recent_rate = recent_success / len(recent) if recent else 0.0

        return {
            "total_launched": self._total_launched,
            "total_completed": self._total_completed,
            "total_failed": self._total_failed,
            "total_detected": self._total_detected,
            "recent_success_rate": round(recent_rate, 4),
            "active_campaigns": sum(1 for c in self._campaigns.values() if c.is_active),
            "total_campaigns": len(self._campaigns),
            # PATCH: stable active_workers
            "active_workers": self._active_workers,
            "max_concurrent": self._max_concurrent,
        }

    def get_campaign(self, campaign_id: str) -> Optional[TrafficCampaign]:
        return self._campaigns.get(campaign_id)

    def get_all_campaigns(self) -> List[TrafficCampaign]:
        return list(self._campaigns.values())

    def get_campaign_metrics(self, campaign_id: str) -> Dict[str, Any]:
        campaign = self._campaigns.get(campaign_id)
        if not campaign:
            return {}
        scheduler = self._schedulers.get(campaign_id)
        scheduler_stats = scheduler.get_stats() if scheduler else {}
        return {"campaign": campaign.to_dict(), "scheduler": scheduler_stats}

    async def _inc_active_workers(self) -> None:
        async with self._active_workers_lock:
            self._active_workers += 1

    async def _dec_active_workers(self) -> None:
        async with self._active_workers_lock:
            self._active_workers = max(0, self._active_workers - 1)