"""
Jubra Traffic Pro - Detection Evader (nodriver Edition)
Advanced anti-bot detection evasion using nodriver's
native CDP access. No Selenium dependency.
"""

import asyncio
import random
import time
import re
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set
from collections import deque
from enum import Enum, auto

try:
    import nodriver as uc
    HAS_NODRIVER = True
except ImportError:
    HAS_NODRIVER = False

from core.event_bus import (
    EventBus,
    EventCategory,
    EventPriority,
    Event,
    get_event_bus,
)
from core.config_manager import ConfigManager

logger = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Detection Systems
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class DetectionSystem(Enum):
    CLOUDFLARE      = "cloudflare"
    AKAMAI          = "akamai"
    PERIMETERX      = "perimeterx"
    DATADOME        = "datadome"
    RECAPTCHA       = "recaptcha"
    HCAPTCHA        = "hcaptcha"
    KASADA          = "kasada"
    SHAPE_SECURITY  = "shape_security"
    GENERIC         = "generic"


@dataclass
class DetectionSignal:
    """A detected anti-bot signal."""
    system:         DetectionSystem
    signal_type:    str
    confidence:     float
    page_url:       str     = ""
    details:        str     = ""
    detected_at:    float   = field(default_factory=time.monotonic)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Detection Evader (nodriver)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class DetectionEvader:
    """
    nodriver Detection Evader.

    Key improvements over Selenium version:
    ─────────────────────────────────────────────────────
    • page.get_content() instead of driver.page_source
    • page.evaluate() instead of driver.execute_script()
    • page.select() instead of find_elements()
    • page.send(cdp.xxx) for all CDP commands
    • page.url for current URL
    • Native async throughout
    • No WebDriver markers to remove
      (nodriver never adds them)
    """

    BLOCK_STATUS_CODES: Set[int] = {403, 429, 503, 520, 521, 522, 524}

    DETECTION_INDICATORS: Dict[str, List[str]] = {
        "cloudflare": [
            "cloudflare", "cf-ray", "checking your browser",
            "just a moment", "cf_chl", "turnstile",
            "enable javascript and cookies",
            "ray id",
        ],
        "akamai": [
            "akamai", "ak_bmsc", "_abck", "bm_sz",
            "bot manager", "akamaibots",
        ],
        "perimeterx": [
            "perimeterx", "px-captcha", "_pxhd", "_pxde",
            "human verification", "pxcaptcha",
        ],
        "datadome": [
            "datadome", "dd_cookie", "datadome_captcha",
            "ddg_", "bot detection",
        ],
        "kasada": [
            "kasada", "kpsdk", "ksada",
        ],
        "recaptcha": [
            "recaptcha", "grecaptcha", "g-recaptcha",
            "recaptcha/api.js",
        ],
        "hcaptcha": [
            "hcaptcha", "h-captcha", "hcaptcha.com",
        ],
        "generic": [
            "access denied", "blocked", "forbidden",
            "bot detected", "automated access",
            "unusual traffic", "suspicious activity",
            "security check", "please verify",
        ],
    }

    def __init__(
        self,
        config:         ConfigManager,
        event_bus:      Optional[EventBus]  = None,
        max_retries:    int                 = 3,
        retry_delay:    float               = 5.0,
    ):
        self._config        = config
        self._event_bus     = event_bus or get_event_bus()
        self._max_retries   = max_retries
        self._retry_delay   = retry_delay

        # Detection history
        self._detections:       deque   = deque(maxlen=200)
        self._total_detected:   int     = 0
        self._total_evaded:     int     = 0

        # Honeypot patterns
        self._honeypot_patterns = [
            "trap", "honeypot", "bot-trap",
            "detect-bot", "spider-trap",
        ]

        logger.info("[DetectionEvader] Initialized (nodriver)")

    # ── Detection ──────────────────────────────────────────

    async def check_for_detection(
        self,
        page:       Any,    # nodriver Tab
        page_url:   str     = "",
    ) -> Optional[DetectionSignal]:
        """
        Analyze current page for anti-bot detection.
        Uses nodriver's page.get_content() and page.evaluate().
        """
        try:
            # Get page source via nodriver
            page_source = await page.get_content() or ""
            page_source_lower = page_source.lower()

            # Get current URL via nodriver
            current_url = page.url or page_url

            # Check each detection system
            for system_name, indicators in self.DETECTION_INDICATORS.items():
                matches = [
                    ind for ind in indicators
                    if ind in page_source_lower
                ]
                if not matches:
                    continue

                confidence = min(1.0, len(matches) / 3)

                try:
                    system = DetectionSystem(system_name)
                except ValueError:
                    system = DetectionSystem.GENERIC

                signal = DetectionSignal(
                    system      = system,
                    signal_type = "page_content",
                    confidence  = confidence,
                    page_url    = current_url,
                    details     = f"Matched: {', '.join(matches[:3])}",
                )

                self._detections.append(signal)
                self._total_detected += 1

                logger.warning(
                    f"[DetectionEvader] Detected: {system_name} | "
                    f"url={current_url[:50]} | "
                    f"confidence={confidence:.2f}"
                )

                await self._event_bus.publish_simple(
                    EventCategory.DETECTION_BOT_DETECTED,
                    {
                        "system":       system_name,
                        "confidence":   confidence,
                        "url":          current_url,
                        "signals":      matches[:3],
                    },
                    priority=EventPriority.HIGH,
                )

                return signal

        except Exception as exc:
            logger.debug(f"[DetectionEvader] Check error: {exc}")

        return None

    async def check_status_code(self, page: Any) -> bool:
        """Check HTTP response status via nodriver CDP."""
        try:
            # Get status from navigation timing
            status = await page.evaluate("""
                (() => {
                    const nav = performance.getEntriesByType('navigation')[0];
                    return nav ? nav.responseStatus : 200;
                })()
            """)
            if status and int(status) in self.BLOCK_STATUS_CODES:
                logger.warning(
                    f"[DetectionEvader] Blocked status: {status}"
                )
                return True
        except Exception:
            pass
        return False

    # ── Evasion Strategies ─────────────────────────────────

    async def evade_cloudflare(
        self,
        page:       Any,
        simulator:  Any,
    ) -> bool:
        """
        Evade Cloudflare protection using nodriver.

        nodriver is naturally much better at this than Selenium
        because it has no ChromeDriver fingerprint.
        """
        logger.info("[DetectionEvader] Cloudflare evasion...")

        try:
            # Wait for challenge to appear
            await asyncio.sleep(random.uniform(3, 6))

            # Check for Turnstile widget
            turnstile = await page.select(
                ".cf-turnstile, [data-sitekey], "
                "#challenge-form, #challenge-stage"
            )

            if turnstile:
                logger.info("[DetectionEvader] Turnstile detected")

                # Move mouse naturally over widget
                await simulator.mouse.move_to_element(turnstile)
                await asyncio.sleep(random.uniform(0.5, 1.5))

                # Natural hover
                await simulator.mouse.move_to(
                    simulator.mouse.current_position[0] +
                    random.uniform(-10, 10),
                    simulator.mouse.current_position[1] +
                    random.uniform(-5, 5),
                )
                await asyncio.sleep(random.uniform(0.5, 1.0))

                # Click the checkbox/widget
                await turnstile.click()
                await asyncio.sleep(random.uniform(3, 6))

            # Wait for challenge resolution
            for attempt in range(15):
                await asyncio.sleep(1.0)

                # Get fresh page content
                source = await page.get_content() or ""
                source_lower = source.lower()

                # Check if challenge is gone
                still_challenged = any(
                    ind in source_lower
                    for ind in [
                        "checking your browser",
                        "cf_chl",
                        "just a moment",
                        "challenge-form",
                    ]
                )

                if not still_challenged:
                    self._total_evaded += 1
                    logger.info(
                        f"[DetectionEvader] Cloudflare evaded "
                        f"(attempt {attempt + 1})"
                    )
                    return True

            logger.warning("[DetectionEvader] Cloudflare evasion failed")
            return False

        except Exception as exc:
            logger.debug(
                f"[DetectionEvader] CF evasion error: {exc}"
            )
            return False

    async def evade_rate_limit(
        self,
        retry_after:    float   = 0,
        base_delay:     float   = 60.0,
    ) -> None:
        """Wait out a rate limit."""
        wait_time   = retry_after if retry_after > 0 else base_delay
        wait_time   += random.uniform(5, 20)
        logger.info(
            f"[DetectionEvader] Rate limited, "
            f"waiting {wait_time:.0f}s"
        )
        await asyncio.sleep(wait_time)

    async def avoid_honeypots(self, page: Any) -> int:
        """Detect honeypot links and return count."""
        count = 0
        try:
            links = await page.select_all("a")

            for link in links:
                try:
                    href = link.attrs.get("href", "")

                    # Check URL patterns
                    if any(
                        p in href.lower()
                        for p in self._honeypot_patterns
                    ):
                        count += 1
                        logger.warning(
                            f"[DetectionEvader] Honeypot URL: {href}"
                        )
                        continue

                    # Check visibility via nodriver
                    style = link.attrs.get("style", "")
                    if any(
                        hidden in style
                        for hidden in [
                            "display:none",
                            "visibility:hidden",
                            "opacity:0",
                        ]
                    ):
                        count += 1

                except Exception:
                    continue

        except Exception as exc:
            logger.debug(f"[DetectionEvader] Honeypot error: {exc}")

        if count > 0:
            logger.info(
                f"[DetectionEvader] Found {count} honeypots"
            )
            await self._event_bus.publish_simple(
                EventCategory.DETECTION_HONEYPOT,
                {"count": count},
                priority=EventPriority.HIGH,
            )

        return count

    async def inject_human_signals(self, page: Any) -> None:
        """
        Inject human behavior signals via nodriver evaluate().
        nodriver doesn't need as many patches as Selenium
        because it naturally avoids most bot indicators.
        """
        script = """
        (function() {
            // Simulate mouse movement history
            if (!window.__humanSignals) {
                window.__humanSignals = {
                    mouseX:          [],
                    mouseY:          [],
                    keystrokes:      [],
                    scrollPositions: [],
                    touchEvents:     [],
                };
            }

            // Add fake mouse history
            for (let i = 0; i < 10; i++) {
                window.__humanSignals.mouseX.push(
                    Math.random() * window.innerWidth
                );
                window.__humanSignals.mouseY.push(
                    Math.random() * window.innerHeight
                );
            }

            // Add scroll history
            let y = 0;
            for (let i = 0; i < 5; i++) {
                y += Math.random() * 100;
                window.__humanSignals.scrollPositions.push(y);
            }

            // PerimeterX interaction signal
            if (!window._pxde) {
                window._pxde = {
                    interactions: true,
                    ts:           Date.now() - Math.random() * 5000,
                };
            }

            // Simulate focus events
            window.dispatchEvent(new Event('focus'));
            document.dispatchEvent(
                new Event('visibilitychange')
            );

            // Fake interaction timing (DataDome checks)
            if (!window.__dd) {
                window.__dd = {
                    firstInteraction: Date.now() - 2000,
                    mouseCount:       Math.floor(Math.random() * 20),
                    keyCount:         Math.floor(Math.random() * 10),
                };
            }

            // Akamai: sensor data placeholder
            if (!window.bmak) {
                window.bmak = {
                    get_telemetry: () => "{}",
                };
            }
        })();
        """
        try:
            await page.evaluate(script)
        except Exception as exc:
            logger.debug(
                f"[DetectionEvader] Signal inject error: {exc}"
            )

    async def apply_proactive_evasion(self, page: Any) -> None:
        """
        Apply proactive evasion on every page load.
        nodriver handles most evasion automatically,
        so this adds extra signals.
        """
        # Inject human signals
        await self.inject_human_signals(page)

        # Small random delay
        await asyncio.sleep(random.uniform(0.1, 0.5))

        # Check for honeypots
        await self.avoid_honeypots(page)

    # ── Handle Detection ───────────────────────────────────

    async def handle_detection(
        self,
        signal:         DetectionSignal,
        page:           Any,
        simulator:      Any,
        captcha_solver: Any = None,
    ) -> bool:
        """Route to appropriate evasion strategy."""
        system = signal.system
        logger.info(
            f"[DetectionEvader] Handling: {system.value} | "
            f"confidence={signal.confidence:.2f}"
        )

        if system == DetectionSystem.CLOUDFLARE:
            return await self.evade_cloudflare(page, simulator)

        elif system in (
            DetectionSystem.RECAPTCHA,
            DetectionSystem.HCAPTCHA,
        ):
            if captcha_solver:
                detection = await captcha_solver.detect_captcha(
                    page, signal.page_url
                )
                if detection:
                    captcha_type, site_key = detection
                    result = await captcha_solver.solve(
                        captcha_type    = captcha_type,
                        site_key        = site_key,
                        page_url        = signal.page_url,
                    )
                    if result:
                        return await self._inject_captcha_token(
                            page, result.token, captcha_type
                        )
            return False

        elif system == DetectionSystem.PERIMETERX:
            # PerimeterX: wait and inject signals
            await asyncio.sleep(random.uniform(3, 8))
            await self.inject_human_signals(page)
            # Try page reload
            await page.reload()
            await asyncio.sleep(random.uniform(2, 5))
            self._total_evaded += 1
            return True

        elif system == DetectionSystem.DATADOME:
            # DataDome: slow down and inject timing signals
            await asyncio.sleep(random.uniform(5, 15))
            await self.inject_human_signals(page)
            self._total_evaded += 1
            return True

        elif system == DetectionSystem.AKAMAI:
            # Akamai: inject sensor data
            await self.inject_human_signals(page)
            await asyncio.sleep(random.uniform(2, 6))
            self._total_evaded += 1
            return True

        else:
            # Generic: wait and retry
            await asyncio.sleep(random.uniform(5, 15))
            await self.inject_human_signals(page)
            self._total_evaded += 1
            return True

    async def _inject_captcha_token(
        self,
        page:           Any,
        token:          str,
        captcha_type:   Any,
    ) -> bool:
        """Inject solved CAPTCHA token via nodriver evaluate()."""
        try:
            script = f"""
            (function() {{
                // reCAPTCHA
                const rcArea = document.querySelector(
                    '#g-recaptcha-response, [name="g-recaptcha-response"]'
                );
                if (rcArea) {{
                    rcArea.style.display = 'block';
                    rcArea.value = '{token}';
                }}

                // Try to trigger callback
                if (typeof ___grecaptcha_cfg !== 'undefined') {{
                    const clients = Object.values(
                        ___grecaptcha_cfg.clients || {{}}
                    );
                    if (clients.length > 0) {{
                        const c = clients[0];
                        for (const [k, v] of Object.entries(c)) {{
                            if (v && typeof v.callback === 'function') {{
                                v.callback('{token}');
                                break;
                            }}
                        }}
                    }}
                }}

                // hCaptcha
                const hcArea = document.querySelector(
                    '[name="h-captcha-response"]'
                );
                if (hcArea) hcArea.value = '{token}';

                // Turnstile
                const tsArea = document.querySelector(
                    '[name="cf-turnstile-response"]'
                );
                if (tsArea) tsArea.value = '{token}';

                return true;
            }})();
            """
            result = await page.evaluate(script)
            return bool(result)

        except Exception as exc:
            logger.error(
                f"[DetectionEvader] Token inject error: {exc}"
            )
            return False

    # ── Page Reload ────────────────────────────────────────

    async def reload_with_delay(
        self,
        page:       Any,
        min_delay:  float = 3.0,
        max_delay:  float = 8.0,
    ) -> None:
        """Reload page with human-like delay."""
        await asyncio.sleep(random.uniform(min_delay, max_delay))
        try:
            await page.reload()
        except Exception as exc:
            logger.debug(f"[DetectionEvader] Reload error: {exc}")

    async def navigate_away_and_back(
        self,
        page:       Any,
        away_url:   str = "https://www.google.com",
        delay:      float = random.uniform(3, 8),
    ) -> None:
        """
        Navigate to an innocent page then back.
        Helps reset detection state on some systems.
        """
        try:
            original_url = page.url or ""
            # Go to innocent page
            await page.get(away_url)
            await asyncio.sleep(delay)
            # Come back
            if original_url:
                await page.get(original_url)
        except Exception as exc:
            logger.debug(
                f"[DetectionEvader] Navigate away error: {exc}"
            )

    # ── Browser-Level Evasion ──────────────────────────────

    async def apply_browser_stealth(self, page: Any) -> None:
        """
        Apply browser-level stealth via nodriver CDP.
        These are applied once per browser session.
        """
        try:
            # Disable automation flags at CDP level
            await page.send(
                uc.cdp.emulation.set_automation_override(
                    enabled=False
                )
            )
        except Exception:
            pass

        try:
            # Override User-Agent via CDP
            pass  # Already handled by BrowserInstance
        except Exception:
            pass

        try:
            # Disable webdriver via CDP
            await page.evaluate("""
                Object.defineProperty(navigator, 'webdriver', {
                    get: () => undefined,
                    configurable: true,
                });
            """)
        except Exception:
            pass

    async def check_and_handle(
        self,
        page:           Any,
        simulator:      Any,
        captcha_solver: Any     = None,
        page_url:       str     = "",
    ) -> bool:
        """
        Convenience method: check for detection and handle if found.
        Returns True if page is clean (no detection or successfully evaded).
        """
        # Check status code
        if await self.check_status_code(page):
            await self.evade_rate_limit()
            return False

        # Check page content
        signal = await self.check_for_detection(page, page_url)

        if signal is None:
            # No detection - apply proactive evasion
            await self.apply_proactive_evasion(page)
            return True

        # Handle detection
        evaded = await self.handle_detection(
            signal          = signal,
            page            = page,
            simulator       = simulator,
            captcha_solver  = captcha_solver,
        )

        if evaded:
            await self._event_bus.publish_simple(
                EventCategory.DETECTION_EVADED,
                {
                    "system":       signal.system.value,
                    "confidence":   signal.confidence,
                    "url":          signal.page_url,
                },
                priority=EventPriority.NORMAL,
            )

        return evaded

    # ── Metrics ────────────────────────────────────────────

    def get_recent_detections(self, count: int = 10) -> List[Dict]:
        recent = list(self._detections)[-count:]
        return [
            {
                "system":       d.system.value,
                "confidence":   round(d.confidence, 3),
                "url":          d.page_url[:50],
                "details":      d.details[:100],
                "age_s":        round(
                    time.monotonic() - d.detected_at, 1
                ),
            }
            for d in recent
        ]

    def get_metrics(self) -> Dict[str, Any]:
        return {
            "engine":           "nodriver",
            "total_detected":   self._total_detected,
            "total_evaded":     self._total_evaded,
            "evasion_rate":     round(
                self._total_evaded / max(1, self._total_detected),
                4,
            ),
            "recent_count":     len(self._detections),
        }