"""
Jubra Traffic Pro - Referral Traffic Engine (nodriver Edition)
"""

import asyncio
import random
import time
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

try:
    import nodriver as uc
    HAS_NODRIVER = True
except ImportError:
    HAS_NODRIVER = False

from core.event_bus import EventBus, EventCategory, get_event_bus
from core.config_manager import ConfigManager

logger = logging.getLogger(__name__)


@dataclass
class ReferralSource:
    """A referring website profile."""
    name:           str
    referrer:       str
    category:       str
    trust_level:    float
    weight:         float   = 1.0


REFERRAL_SOURCES = [
    ReferralSource("Medium",        "https://medium.com/@/",        "blog",      0.85, 2.5),
    ReferralSource("HackerNews",    "https://news.ycombinator.com/","forum",     0.92, 1.8),
    ReferralSource("Reddit",        "https://www.reddit.com/r/",    "forum",     0.80, 2.0),
    ReferralSource("Yelp",          "https://www.yelp.com/biz/",    "directory", 0.90, 2.0),
    ReferralSource("G2",            "https://www.g2.com/products/", "partner",   0.88, 1.6),
    ReferralSource("Capterra",      "https://www.capterra.com/p/",  "partner",   0.87, 1.4),
    ReferralSource("Quora",         "https://www.quora.com/",       "forum",     0.80, 1.8),
    ReferralSource("ProductHunt",   "https://www.producthunt.com/", "directory", 0.90, 1.3),
    ReferralSource("LinkedIn",      "https://www.linkedin.com/",    "social",    0.85, 1.5),
    ReferralSource("Substack",      "https://substack.com/",        "blog",      0.80, 1.5),
]


class ReferralTrafficEngine:
    """nodriver Referral Traffic Engine."""

    def __init__(
        self,
        config:         ConfigManager,
        event_bus:      Optional[EventBus]  = None,
        target_urls:    Optional[List[str]] = None,
    ):
        self._config        = config
        self._event_bus     = event_bus or get_event_bus()
        self._target_urls   = target_urls or []
        self._sources       = REFERRAL_SOURCES.copy()
        self._total_visits: int = 0
        self._total_success: int = 0

    def _select_source(self) -> ReferralSource:
        weights = [s.weight * s.trust_level for s in self._sources]
        total   = sum(weights)
        r       = random.uniform(0, total)
        cumul   = 0.0
        for s, w in zip(self._sources, weights):
            cumul += w
            if r <= cumul:
                return s
        return self._sources[0]

    async def execute_referral_visit(
        self,
        session:    Any,
        browser:    Any,
        simulator:  Any,
        target_url: str             = "",
        source_name: Optional[str]  = None,
    ) -> Dict[str, Any]:
        """Execute a referral traffic visit."""
        result = {
            "success":          False,
            "source":           "",
            "target_url":       "",
            "time_on_site":     0.0,
            "pages_visited":    0,
            "error":            None,
        }

        try:
            # Select source
            source = next(
                (s for s in self._sources if s.name == source_name),
                self._select_source()
            )
            result["source"] = source.name

            url = target_url or (
                random.choice(self._target_urls)
                if self._target_urls else ""
            )
            if not url:
                result["error"] = "No target URL"
                return result

            result["target_url"] = url

            # Set Referer header
            await self._set_referer(browser._page, source.referrer)

            # Navigate
            page = await browser._browser.get(url)
            simulator.update_page(page)
            browser._page = page
            result["pages_visited"] += 1

            start_time  = time.monotonic()
            engagement  = source.trust_level * random.uniform(0.7, 1.0)

            # Read page
            word_count  = await simulator.get_word_count()
            await simulator.simulate_page_read(
                content_type    = self._category_to_content(
                    source.category
                ),
                word_count      = word_count,
                scroll          = True,
                interact        = engagement > 0.7,
            )

            # Source-specific behavior
            await self._source_behavior(
                browser._page, simulator, source
            )

            result["time_on_site"]  = time.monotonic() - start_time
            result["success"]       = True
            self._total_visits  += 1
            self._total_success += 1

            await self._event_bus.publish_simple(
                EventCategory.TRAFFIC_VISIT_COMPLETE,
                {
                    "type":     "referral",
                    "source":   source.name,
                    "url":      url,
                    "time":     round(result["time_on_site"], 2),
                },
                session_id=session.session_id,
            )

        except Exception as exc:
            result["error"] = str(exc)
            logger.error(f"[ReferralTrafficEngine] Error: {exc}")

        return result

    async def _set_referer(self, page: Any, referrer: str) -> None:
        try:
            await page.send(
                uc.cdp.network.set_extra_http_headers(
                    headers=uc.cdp.network.Headers(
                        {"Referer": referrer}
                    )
                )
            )
        except Exception as exc:
            logger.debug(f"[ReferralTrafficEngine] Referer: {exc}")

    async def _source_behavior(
        self,
        page:       Any,
        simulator:  Any,
        source:     ReferralSource,
    ) -> None:
        """Apply source-specific behavior."""
        if source.category == "forum":
            await asyncio.sleep(random.uniform(5, 15))
            await simulator.scroll.scroll_by(
                random.randint(100, 300)
            )
            await asyncio.sleep(random.uniform(10, 30))

        elif source.category == "directory":
            await asyncio.sleep(random.uniform(2, 8))

        elif source.category == "blog":
            await asyncio.sleep(random.uniform(8, 20))

        elif source.category == "partner":
            await asyncio.sleep(random.uniform(5, 15))
            try:
                cta = await page.select(
                    ".cta, .pricing, [class*='price'], .buy-button"
                )
                if cta:
                    await simulator.scroll.scroll_to_element(cta)
                    await simulator.mouse.move_to_element(cta)
                    await asyncio.sleep(random.uniform(2, 5))
            except Exception:
                pass

    @staticmethod
    def _category_to_content(category: str) -> str:
        return {
            "blog":      "article",
            "forum":     "article",
            "directory": "product",
            "partner":   "product",
        }.get(category, "article")

    def get_metrics(self) -> Dict[str, Any]:
        return {
            "total_visits":  self._total_visits,
            "total_success": self._total_success,
        }