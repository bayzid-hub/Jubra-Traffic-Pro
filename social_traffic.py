"""
Jubra Traffic Pro - Social Traffic Engine (nodriver Edition)
"""

import asyncio
import random
import time
import logging
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse, urlencode

try:
    import nodriver as uc
    HAS_NODRIVER = True
except ImportError:
    HAS_NODRIVER = False

from core.event_bus import EventBus, EventCategory, get_event_bus
from core.config_manager import ConfigManager

logger = logging.getLogger(__name__)


SOCIAL_PLATFORMS = {
    "facebook": {
        "name":         "Facebook",
        "referrer":     "https://www.facebook.com/",
        "utm_source":   "facebook",
        "utm_medium":   "social",
        "weight":       3.5,
        "mobile_ratio": 0.70,
    },
    "twitter": {
        "name":         "Twitter/X",
        "referrer":     "https://t.co/",
        "utm_source":   "twitter",
        "utm_medium":   "social",
        "weight":       2.0,
        "mobile_ratio": 0.65,
    },
    "instagram": {
        "name":         "Instagram",
        "referrer":     "https://www.instagram.com/",
        "utm_source":   "instagram",
        "utm_medium":   "social",
        "weight":       2.5,
        "mobile_ratio": 0.85,
    },
    "linkedin": {
        "name":         "LinkedIn",
        "referrer":     "https://www.linkedin.com/",
        "utm_source":   "linkedin",
        "utm_medium":   "social",
        "weight":       1.5,
        "mobile_ratio": 0.40,
    },
    "reddit": {
        "name":         "Reddit",
        "referrer":     "https://www.reddit.com/",
        "utm_source":   "reddit",
        "utm_medium":   "social",
        "weight":       1.8,
        "mobile_ratio": 0.50,
    },
    "tiktok": {
        "name":         "TikTok",
        "referrer":     "https://www.tiktok.com/",
        "utm_source":   "tiktok",
        "utm_medium":   "social",
        "weight":       1.6,
        "mobile_ratio": 0.90,
    },
}


class SocialTrafficEngine:
    """nodriver Social Traffic Engine."""

    def __init__(
        self,
        config:         ConfigManager,
        event_bus:      Optional[EventBus]  = None,
        target_urls:    Optional[List[str]] = None,
    ):
        self._config        = config
        self._event_bus     = event_bus or get_event_bus()
        self._target_urls   = target_urls or []
        self._total_visits: int = 0
        self._total_success: int = 0

    def _select_platform(self) -> Dict:
        platforms   = list(SOCIAL_PLATFORMS.values())
        weights     = [p["weight"] for p in platforms]
        total       = sum(weights)
        r           = random.uniform(0, total)
        cumul       = 0.0
        for p, w in zip(platforms, weights):
            cumul += w
            if r <= cumul:
                return p
        return platforms[0]

    def _build_url(self, base_url: str, platform: Dict) -> str:
        utm = {
            "utm_source":   platform["utm_source"],
            "utm_medium":   platform["utm_medium"],
            "utm_campaign": random.choice([
                "social_post", "brand_awareness",
                "engagement", "product_launch",
            ]),
        }
        sep = "&" if "?" in base_url else "?"
        return base_url + sep + urlencode(utm)

    async def execute_social_visit(
        self,
        session:    Any,
        browser:    Any,
        simulator:  Any,
        target_url: str             = "",
        platform_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Execute a social media referral visit."""
        result = {
            "success":      False,
            "platform":     "",
            "target_url":   "",
            "time_on_site": 0.0,
            "pages_visited": 0,
            "error":        None,
        }

        try:
            # Select platform
            if platform_name and platform_name in SOCIAL_PLATFORMS:
                platform = SOCIAL_PLATFORMS[platform_name]
            else:
                platform = self._select_platform()

            result["platform"] = platform["name"]

            # Target URL
            url = target_url or (
                random.choice(self._target_urls)
                if self._target_urls else ""
            )
            if not url:
                result["error"] = "No target URL"
                return result

            # Build URL with UTM params
            full_url = self._build_url(url, platform)
            result["target_url"] = full_url

            # ── Set referrer via CDP ───────────────────────
            await self._set_referrer(browser._page, platform["referrer"])

            # ── Navigate to target ─────────────────────────
            page = await browser._browser.get(full_url)
            simulator.update_page(page)
            browser._page = page
            result["pages_visited"] += 1

            start_time = time.monotonic()

            # ── Social-style behavior ──────────────────────
            # Social users scroll fast at first
            await asyncio.sleep(random.uniform(1.0, 2.5))

            # Quick initial scroll (social media = fast scrollers)
            await simulator.scroll.scroll_by(
                random.randint(100, 300)
            )
            await asyncio.sleep(random.uniform(0.5, 1.5))

            # ── Read page ─────────────────────────────────
            word_count  = await simulator.get_word_count()
            content     = self._guess_content_type(url)

            await simulator.simulate_page_read(
                content_type    = content,
                word_count      = word_count,
                scroll          = True,
                interact        = True,
            )

            # ── Social sharing behavior ───────────────────
            if random.random() < 0.15:
                await self._hover_share_buttons(
                    browser._page, simulator
                )

            # ── Possible second page ──────────────────────
            if random.random() < 0.35:
                navigated = await self._navigate_internal(
                    browser, simulator, url
                )
                if navigated:
                    result["pages_visited"] += 1
                    await asyncio.sleep(random.uniform(15, 45))

            result["time_on_site"]  = time.monotonic() - start_time
            result["success"]       = True

            self._total_visits  += 1
            self._total_success += 1

            await self._event_bus.publish_simple(
                EventCategory.TRAFFIC_VISIT_COMPLETE,
                {
                    "type":         "social",
                    "platform":     platform["name"],
                    "url":          full_url,
                    "time_on_site": round(result["time_on_site"], 2),
                },
                session_id=session.session_id,
            )

        except Exception as exc:
            result["error"] = str(exc)
            logger.error(f"[SocialTrafficEngine] Error: {exc}")

        return result

    async def _set_referrer(self, page: Any, referrer: str) -> None:
        """Set Referer header via CDP."""
        try:
            await page.send(
                uc.cdp.network.set_extra_http_headers(
                    headers=uc.cdp.network.Headers(
                        {"Referer": referrer}
                    )
                )
            )
        except Exception as exc:
            logger.debug(f"[SocialTrafficEngine] Referer error: {exc}")

    async def _hover_share_buttons(
        self,
        page:       Any,
        simulator:  Any,
    ) -> None:
        """Hover over social share buttons."""
        try:
            share_selectors = [
                "[class*='share']",
                "[class*='social']",
                ".share-button",
            ]
            for selector in share_selectors:
                elements = await page.select_all(selector)
                if elements:
                    el = random.choice(elements[:3])
                    await simulator.mouse.move_to_element(el)
                    await asyncio.sleep(random.uniform(0.3, 1.0))
                    break
        except Exception:
            pass

    async def _navigate_internal(
        self,
        browser:    Any,
        simulator:  Any,
        base_url:   str,
    ) -> bool:
        """Navigate to an internal link."""
        try:
            domain  = urlparse(base_url).netloc
            links   = await browser._page.select_all("a")
            internal = [
                l for l in links
                if domain in (l.attrs.get("href", ""))
            ]
            if not internal:
                return False

            link = random.choice(internal[:5])
            await simulator.simulate_link_click(link)
            simulator.update_page(browser._page)
            return True

        except Exception:
            return False

    def _guess_content_type(self, url: str) -> str:
        url_lower = url.lower()
        if any(w in url_lower for w in ["blog", "post", "article"]):
            return "article"
        if any(w in url_lower for w in ["product", "item", "shop"]):
            return "product"
        return "article"

    def get_metrics(self) -> Dict[str, Any]:
        return {
            "total_visits":  self._total_visits,
            "total_success": self._total_success,
            "success_rate":  round(
                self._total_success / max(1, self._total_visits), 4
            ),
        }