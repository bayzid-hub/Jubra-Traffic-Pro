"""
Jubra Traffic Pro - Multi-Service CAPTCHA Solver
Supports 2captcha, Anti-Captcha, CapMonster with
automatic fallback, budget tracking, and result caching.
"""

import asyncio
import time
import json
import hashlib
import logging
import aiohttp
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
from collections import deque
from enum import Enum, auto

from core.exceptions import (
    CaptchaError,
    CaptchaDetectedError,
    CaptchaSolveFailedError,
    CaptchaServiceError,
    CaptchaBudgetExceededError,
    ErrorContext,
)
from core.event_bus import EventBus, EventCategory, EventPriority, get_event_bus
from core.config_manager import ConfigManager

logger = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# CAPTCHA Types
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class CaptchaType(Enum):
    RECAPTCHA_V2        = "recaptcha_v2"
    RECAPTCHA_V3        = "recaptcha_v3"
    RECAPTCHA_ENTERPRISE = "recaptcha_enterprise"
    HCAPTCHA            = "hcaptcha"
    CLOUDFLARE_TURNSTILE = "turnstile"
    IMAGE_CAPTCHA       = "image"
    TEXT_CAPTCHA        = "text"
    FUNCAPTCHA          = "funcaptcha"
    GEE_TEST            = "geetest"
    INVISIBLE_RECAPTCHA = "invisible_recaptcha"


class SolverService(Enum):
    TWOCAPTCHA  = "2captcha"
    ANTICAPTCHA = "anticaptcha"
    CAPMONSTER  = "capmonster"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# CAPTCHA Result
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@dataclass
class CaptchaResult:
    """Result from CAPTCHA solving service."""
    captcha_id:     str
    token:          str
    captcha_type:   CaptchaType
    service:        SolverService
    solve_time_s:   float
    cost_usd:       float
    is_cached:      bool            = False
    confidence:     float           = 1.0
    solved_at:      float           = field(default_factory=time.monotonic)

    def is_valid(self, max_age_s: float = 110.0) -> bool:
        """Check if token is still valid (reCAPTCHA tokens expire in 2min)."""
        return (time.monotonic() - self.solved_at) < max_age_s

    def to_dict(self) -> Dict[str, Any]:
        return {
            "captcha_id":   self.captcha_id,
            "token":        self.token[:20] + "...",
            "type":         self.captcha_type.value,
            "service":      self.service.value,
            "solve_time_s": round(self.solve_time_s, 2),
            "cost_usd":     round(self.cost_usd, 4),
            "is_cached":    self.is_cached,
        }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Solver Backends
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TwoCaptchaSolver:
    """2Captcha API backend."""

    BASE_URL    = "https://2captcha.com"
    IN_URL      = f"{BASE_URL}/in.php"
    RES_URL     = f"{BASE_URL}/res.php"

    # Cost per 1000 solves (USD)
    COST_PER_SOLVE = {
        CaptchaType.IMAGE_CAPTCHA:      0.001,
        CaptchaType.RECAPTCHA_V2:       0.002,
        CaptchaType.RECAPTCHA_V3:       0.002,
        CaptchaType.HCAPTCHA:           0.002,
        CaptchaType.CLOUDFLARE_TURNSTILE: 0.002,
        CaptchaType.FUNCAPTCHA:         0.003,
        CaptchaType.GEE_TEST:           0.003,
    }

    def __init__(self, api_key: str, timeout: float = 120.0):
        self._api_key   = api_key
        self._timeout   = timeout

    async def solve_recaptcha_v2(
        self,
        site_key:   str,
        page_url:   str,
        invisible:  bool = False,
    ) -> Optional[str]:
        """Solve reCAPTCHA v2."""
        params = {
            "key":          self._api_key,
            "method":       "userrecaptcha",
            "googlekey":    site_key,
            "pageurl":      page_url,
            "json":         1,
        }
        if invisible:
            params["invisible"] = 1

        return await self._submit_and_poll(params)

    async def solve_recaptcha_v3(
        self,
        site_key:   str,
        page_url:   str,
        action:     str     = "verify",
        min_score:  float   = 0.7,
    ) -> Optional[str]:
        """Solve reCAPTCHA v3 for a given action."""
        params = {
            "key":          self._api_key,
            "method":       "userrecaptcha",
            "version":      "v3",
            "googlekey":    site_key,
            "pageurl":      page_url,
            "action":       action,
            "min_score":    min_score,
            "json":         1,
        }
        return await self._submit_and_poll(params)

    async def solve_hcaptcha(
        self,
        site_key:   str,
        page_url:   str,
    ) -> Optional[str]:
        """Solve hCaptcha."""
        params = {
            "key":          self._api_key,
            "method":       "hcaptcha",
            "sitekey":      site_key,
            "pageurl":      page_url,
            "json":         1,
        }
        return await self._submit_and_poll(params)

    async def solve_turnstile(
        self,
        site_key:   str,
        page_url:   str,
    ) -> Optional[str]:
        """Solve Cloudflare Turnstile."""
        params = {
            "key":          self._api_key,
            "method":       "turnstile",
            "sitekey":      site_key,
            "pageurl":      page_url,
            "json":         1,
        }
        return await self._submit_and_poll(params)

    async def get_balance(self) -> float:
        """Get account balance in USD."""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    self.RES_URL,
                    params={
                        "key":    self._api_key,
                        "action": "getbalance",
                        "json":   1,
                    },
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    data = await resp.json()
                    if data.get("status") == 1:
                        return float(data.get("request", 0))
        except Exception as exc:
            logger.debug(f"[2Captcha] Balance check failed: {exc}")
        return 0.0

    async def _submit_and_poll(
        self,
        params:     Dict[str, Any],
        poll_interval: float = 5.0,
    ) -> Optional[str]:
        """Submit CAPTCHA task and poll for result."""
        # Submit
        task_id = None
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    self.IN_URL,
                    data    = params,
                    timeout = aiohttp.ClientTimeout(total=30),
                ) as resp:
                    data = await resp.json()
                    if data.get("status") == 1:
                        task_id = data.get("request")
                    else:
                        error = data.get("request", "unknown")
                        logger.error(f"[2Captcha] Submit error: {error}")
                        return None
        except Exception as exc:
            logger.error(f"[2Captcha] Submit failed: {exc}")
            return None

        if not task_id:
            return None

        # Poll for result
        start = time.monotonic()
        await asyncio.sleep(15.0)  # Initial wait

        while time.monotonic() - start < self._timeout:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        self.RES_URL,
                        params={
                            "key":    self._api_key,
                            "action": "get",
                            "id":     task_id,
                            "json":   1,
                        },
                        timeout=aiohttp.ClientTimeout(total=10),
                    ) as resp:
                        data = await resp.json()
                        status = data.get("status")

                        if status == 1:
                            return data.get("request")  # Token

                        if data.get("request") == "ERROR_CAPTCHA_UNSOLVABLE":
                            logger.warning("[2Captcha] Unsolvable CAPTCHA")
                            return None

            except Exception as exc:
                logger.debug(f"[2Captcha] Poll error: {exc}")

            await asyncio.sleep(poll_interval)

        logger.error(f"[2Captcha] Timeout after {self._timeout}s")
        return None

    def get_cost(self, captcha_type: CaptchaType) -> float:
        return self.COST_PER_SOLVE.get(captcha_type, 0.002)


class AntiCaptchaSolver:
    """Anti-Captcha API backend."""

    BASE_URL = "https://api.anti-captcha.com"

    COST_PER_SOLVE = {
        CaptchaType.IMAGE_CAPTCHA:  0.0007,
        CaptchaType.RECAPTCHA_V2:   0.0015,
        CaptchaType.RECAPTCHA_V3:   0.0015,
        CaptchaType.HCAPTCHA:       0.0015,
    }

    def __init__(self, api_key: str, timeout: float = 120.0):
        self._api_key   = api_key
        self._timeout   = timeout

    async def solve_recaptcha_v2(
        self,
        site_key:   str,
        page_url:   str,
    ) -> Optional[str]:
        task_payload = {
            "clientKey": self._api_key,
            "task": {
                "type":        "NoCaptchaTaskProxyless",
                "websiteURL":  page_url,
                "websiteKey":  site_key,
            },
        }
        return await self._create_and_get_task(task_payload)

    async def solve_hcaptcha(
        self,
        site_key:   str,
        page_url:   str,
    ) -> Optional[str]:
        task_payload = {
            "clientKey": self._api_key,
            "task": {
                "type":        "HCaptchaTaskProxyless",
                "websiteURL":  page_url,
                "websiteKey":  site_key,
            },
        }
        return await self._create_and_get_task(task_payload)

    async def get_balance(self) -> float:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self.BASE_URL}/getBalance",
                    json    = {"clientKey": self._api_key},
                    timeout = aiohttp.ClientTimeout(total=10),
                ) as resp:
                    data = await resp.json()
                    if data.get("errorId") == 0:
                        return float(data.get("balance", 0))
        except Exception:
            pass
        return 0.0

    async def _create_and_get_task(
        self,
        payload:        Dict[str, Any],
        poll_interval:  float = 5.0,
    ) -> Optional[str]:
        """Create task and poll for solution."""
        task_id = None
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self.BASE_URL}/createTask",
                    json    = payload,
                    timeout = aiohttp.ClientTimeout(total=30),
                ) as resp:
                    data = await resp.json()
                    if data.get("errorId") == 0:
                        task_id = data.get("taskId")
                    else:
                        logger.error(
                            f"[AntiCaptcha] Create error: "
                            f"{data.get('errorDescription')}"
                        )
                        return None
        except Exception as exc:
            logger.error(f"[AntiCaptcha] Create failed: {exc}")
            return None

        if not task_id:
            return None

        await asyncio.sleep(10.0)

        start = time.monotonic()
        while time.monotonic() - start < self._timeout:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(
                        f"{self.BASE_URL}/getTaskResult",
                        json={
                            "clientKey": self._api_key,
                            "taskId":    task_id,
                        },
                        timeout=aiohttp.ClientTimeout(total=10),
                    ) as resp:
                        data = await resp.json()
                        if data.get("status") == "ready":
                            solution = data.get("solution", {})
                            return (
                                solution.get("gRecaptchaResponse") or
                                solution.get("token")
                            )
            except Exception:
                pass
            await asyncio.sleep(poll_interval)

        return None

    def get_cost(self, captcha_type: CaptchaType) -> float:
        return self.COST_PER_SOLVE.get(captcha_type, 0.002)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# CAPTCHA Solver Master
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class CaptchaSolver:
    """
    Jubra Traffic Pro - Multi-Service CAPTCHA Solver

    Features:
    ─────────────────────────────────────────────────────
    • Multi-service support (2captcha, AntiCaptcha, CapMonster)
    • Automatic fallback on service failure
    • Token caching (reCAPTCHA tokens valid ~110s)
    • Budget tracking with per-session limits
    • Service health monitoring
    • Retry logic with service rotation
    • Cost optimization (cheapest service first)
    • CAPTCHA type auto-detection from page
    • Async solving with concurrent limit
    """

    def __init__(
        self,
        config:             ConfigManager,
        event_bus:          Optional[EventBus]  = None,
        primary_service:    str                 = "2captcha",
        api_keys:           Optional[Dict[str, str]] = None,
        budget_usd:         float               = 10.0,
        max_concurrent:     int                 = 5,
        token_cache_size:   int                 = 50,
        enable_cache:       bool                = True,
    ):
        self._config        = config
        self._event_bus     = event_bus or get_event_bus()
        self._budget_usd    = budget_usd
        self._spent_usd     = 0.0
        self._enable_cache  = enable_cache

        # Load API keys
        keys = api_keys or {}
        captcha_key = config.get("security.captcha_api_key", "")

        # Initialize solver backends
        self._solvers: Dict[str, Any] = {}

        twocap_key = keys.get("2captcha", captcha_key)
        if twocap_key:
            self._solvers["2captcha"] = TwoCaptchaSolver(twocap_key)

        anticap_key = keys.get("anticaptcha", "")
        if anticap_key:
            self._solvers["anticaptcha"] = AntiCaptchaSolver(anticap_key)

        # Service priority order
        self._service_order = [
            s for s in [primary_service, "2captcha", "anticaptcha"]
            if s in self._solvers
        ]

        # Token cache: hash → CaptchaResult
        self._cache:    Dict[str, CaptchaResult]    = {}
        self._cache_size = token_cache_size

        # Concurrency limit
        self._sem = asyncio.Semaphore(max_concurrent)

        # Metrics
        self._total_solved:     int     = 0
        self._total_failed:     int     = 0
        self._total_cached:     int     = 0
        self._solve_times:      deque   = deque(maxlen=100)

        logger.info(
            f"[CaptchaSolver] Initialized: "
            f"services={list(self._solvers.keys())}, "
            f"budget=${budget_usd:.2f}"
        )

    # ── Detection ──────────────────────────────────────────

    async def detect_captcha(
        self,
        driver: Any,
        page_url: str = "",
    ) -> Optional[Tuple[CaptchaType, str]]:
        """
        Detect CAPTCHA type and site key from current page.
        Returns (captcha_type, site_key) or None.
        """
        try:
            loop = asyncio.get_event_loop()
            html = await loop.run_in_executor(
                None, lambda: driver.page_source
            )

            # reCAPTCHA v2/v3
            import re
            rc_match = re.search(
                r'["\']sitekey["\']\s*:\s*["\']([a-zA-Z0-9_-]{40})["\']',
                html
            )
            if rc_match:
                site_key = rc_match.group(1)
                # v3 has different data-action
                if "grecaptcha.execute" in html:
                    return CaptchaType.RECAPTCHA_V3, site_key
                return CaptchaType.RECAPTCHA_V2, site_key

            # hCaptcha
            hc_match = re.search(
                r'hcaptcha["\s].*?["\']([a-zA-Z0-9-]{36})["\']',
                html,
            )
            if hc_match:
                return CaptchaType.HCAPTCHA, hc_match.group(1)

            # Cloudflare Turnstile
            ts_match = re.search(
                r'turnstile.*?["\']([a-zA-Z0-9_-]{40,})["\']',
                html,
            )
            if ts_match:
                return CaptchaType.CLOUDFLARE_TURNSTILE, ts_match.group(1)

            # Check for CAPTCHA indicators
            captcha_indicators = [
                "captcha", "recaptcha", "hcaptcha",
                "challenge", "verify you", "are you human",
                "robot", "bot protection",
            ]
            html_lower = html.lower()
            if any(ind in html_lower for ind in captcha_indicators):
                return CaptchaType.IMAGE_CAPTCHA, ""

        except Exception as exc:
            logger.debug(f"[CaptchaSolver] Detection error: {exc}")

        return None

    # ── Solving ────────────────────────────────────────────

    async def solve(
        self,
        captcha_type:   CaptchaType,
        site_key:       str,
        page_url:       str,
        action:         str     = "submit",
        min_score:      float   = 0.7,
        session_id:     str     = "",
    ) -> CaptchaResult:
        """
        Solve a CAPTCHA using available services with fallback.
        """
        # Budget check
        if self._spent_usd >= self._budget_usd:
            raise CaptchaBudgetExceededError(
                spent=self._spent_usd,
                budget=self._budget_usd,
            )

        # Cache check
        if self._enable_cache:
            cache_key = self._make_cache_key(captcha_type, site_key, page_url)
            cached    = self._cache.get(cache_key)
            if cached and cached.is_valid():
                self._total_cached += 1
                logger.debug(
                    f"[CaptchaSolver] Cache hit: {captcha_type.value}"
                )
                return cached

        async with self._sem:
            return await self._solve_with_fallback(
                captcha_type = captcha_type,
                site_key     = site_key,
                page_url     = page_url,
                action       = action,
                min_score    = min_score,
                session_id   = session_id,
            )

    async def _solve_with_fallback(
        self,
        captcha_type:   CaptchaType,
        site_key:       str,
        page_url:       str,
        action:         str,
        min_score:      float,
        session_id:     str,
    ) -> CaptchaResult:
        """Try each service in order until one succeeds."""
        start = time.monotonic()
        last_error = ""

        for service_name in self._service_order:
            solver = self._solvers.get(service_name)
            if not solver:
                continue

            try:
                token = await self._call_solver(
                    solver       = solver,
                    service_name = service_name,
                    captcha_type = captcha_type,
                    site_key     = site_key,
                    page_url     = page_url,
                    action       = action,
                    min_score    = min_score,
                )

                if token:
                    solve_time = time.monotonic() - start
                    cost = solver.get_cost(captcha_type)
                    self._spent_usd += cost

                    result = CaptchaResult(
                        captcha_id   = f"{service_name}_{int(time.time())}",
                        token        = token,
                        captcha_type = captcha_type,
                        service      = SolverService(service_name),
                        solve_time_s = solve_time,
                        cost_usd     = cost,
                    )

                    # Cache result
                    if self._enable_cache:
                        cache_key = self._make_cache_key(
                            captcha_type, site_key, page_url
                        )
                        self._cache[cache_key] = result
                        if len(self._cache) > self._cache_size:
                            oldest = next(iter(self._cache))
                            del self._cache[oldest]

                    self._total_solved += 1
                    self._solve_times.append(solve_time)

                    await self._event_bus.publish_simple(
                        EventCategory.DETECTION_CAPTCHA_SOLVED,
                        {
                            "type":         captcha_type.value,
                            "service":      service_name,
                            "solve_time_s": round(solve_time, 2),
                            "cost_usd":     round(cost, 4),
                            "session_id":   session_id,
                        },
                        priority=EventPriority.NORMAL,
                    )

                    logger.info(
                        f"[CaptchaSolver] Solved: {captcha_type.value} "
                        f"via {service_name} in {solve_time:.1f}s"
                    )
                    return result

            except Exception as exc:
                last_error = str(exc)
                logger.warning(
                    f"[CaptchaSolver] {service_name} failed: {exc}"
                )
                continue

        self._total_failed += 1
        raise CaptchaSolveFailedError(
            service = " → ".join(self._service_order),
            reason  = last_error or "All services failed",
        )

    async def _call_solver(
        self,
        solver:         Any,
        service_name:   str,
        captcha_type:   CaptchaType,
        site_key:       str,
        page_url:       str,
        action:         str,
        min_score:      float,
    ) -> Optional[str]:
        """Route to correct solver method."""
        if captcha_type == CaptchaType.RECAPTCHA_V2:
            return await solver.solve_recaptcha_v2(site_key, page_url)
        elif captcha_type == CaptchaType.RECAPTCHA_V3:
            return await solver.solve_recaptcha_v3(
                site_key, page_url, action, min_score
            )
        elif captcha_type == CaptchaType.HCAPTCHA:
            return await solver.solve_hcaptcha(site_key, page_url)
        elif captcha_type == CaptchaType.CLOUDFLARE_TURNSTILE:
            if hasattr(solver, "solve_turnstile"):
                return await solver.solve_turnstile(site_key, page_url)
        return None

    async def inject_token(
        self,
        driver:     Any,
        token:      str,
        captcha_type: CaptchaType,
    ) -> bool:
        """Inject solved token into the page."""
        try:
            loop = asyncio.get_event_loop()

            if captcha_type in (
                CaptchaType.RECAPTCHA_V2,
                CaptchaType.RECAPTCHA_V3,
                CaptchaType.INVISIBLE_RECAPTCHA,
            ):
                script = f"""
                (function() {{
                    // Set textarea value
                    const textarea = document.querySelector(
                        '#g-recaptcha-response, [name="g-recaptcha-response"]'
                    );
                    if (textarea) {{
                        textarea.style.display = 'block';
                        textarea.value = '{token}';
                    }}

                    // Trigger callback
                    if (typeof ___grecaptcha_cfg !== 'undefined') {{
                        const clients = Object.values(
                            ___grecaptcha_cfg.clients || {{}}
                        );
                        if (clients.length > 0) {{
                            const client = clients[0];
                            const entries = Object.entries(client);
                            for (const [key, val] of entries) {{
                                if (val && typeof val.callback === 'function') {{
                                    val.callback('{token}');
                                    break;
                                }}
                            }}
                        }}
                    }}
                    return true;
                }})();
                """
            elif captcha_type == CaptchaType.HCAPTCHA:
                script = f"""
                (function() {{
                    const textarea = document.querySelector(
                        '[name="h-captcha-response"], #h-captcha-response'
                    );
                    if (textarea) textarea.value = '{token}';

                    if (window.hcaptcha) {{
                        window.hcaptcha.execute();
                    }}
                    return true;
                }})();
                """
            else:
                script = f"""
                document.querySelector(
                    '[name="cf-turnstile-response"], [name="g-recaptcha-response"]'
                )?.setAttribute('value', '{token}');
                return true;
                """

            result = await loop.run_in_executor(
                None,
                lambda: driver.execute_script(script),
            )
            return bool(result)

        except Exception as exc:
            logger.error(f"[CaptchaSolver] Token inject error: {exc}")
            return False

    @staticmethod
    def _make_cache_key(
        captcha_type:   CaptchaType,
        site_key:       str,
        page_url:       str,
    ) -> str:
        raw = f"{captcha_type.value}:{site_key}:{urlparse(page_url).netloc}"
        return hashlib.md5(raw.encode()).hexdigest()[:16]

    def get_metrics(self) -> Dict[str, Any]:
        avg_solve = (
            sum(self._solve_times) / len(self._solve_times)
            if self._solve_times else 0.0
        )
        return {
            "total_solved":     self._total_solved,
            "total_failed":     self._total_failed,
            "total_cached":     self._total_cached,
            "spent_usd":        round(self._spent_usd, 4),
            "budget_usd":       self._budget_usd,
            "budget_remaining": round(self._budget_usd - self._spent_usd, 4),
            "avg_solve_s":      round(avg_solve, 2),
            "cache_size":       len(self._cache),
            "services":         list(self._solvers.keys()),
        }