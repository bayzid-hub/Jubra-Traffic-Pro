"""
Jubra Traffic Pro - Direct Traffic Engine (nodriver Edition)
"""

import asyncio
import random
import time
import logging
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


class DirectTrafficEngine:
    """nodriver Direct Traffic Engine."""

    RETURNING_PATTERNS = [
        "fast_scroll",
        "direct_click",
        "navigation_bar",
        "search_internal",
        "footer_check",
    ]

    def __init__(
        self,
        config:         ConfigManager,
        event_bus:      Optional[EventBus]  = None,
        target_urls:    Optional[List[str]] = None,
        returning_ratio: float              = 0.40,
    ):
        self._config        = config
        self._event_bus     = event_bus or get_event_bus()
        self._target_urls   = target_urls or []
        self._returning_ratio = returning_ratio
        self._total_visits: int = 0
        self._total_success: int = 0

    async def execute_direct_visit(
        self,
        session:    Any,
        browser:    Any,
        simulator:  Any,
        target_url: str = "",
    ) -> Dict[str, Any]:
        """Execute a direct traffic visit."""
        result = {
            "success":          False,
            "target_url":       "",
            "is_returning":     False,
            "pattern":          "",
            "time_on_site":     0.0,
            "pages_visited":    0,
            "error":            None,
        }

        try:
            url = target_url or (
                random.choice(self._target_urls)
                if self._target_urls else ""
            )
            if not url:
                result["error"] = "No target URL"
                return result

            result["target_url"] = url

            # Clear referrer for direct traffic
            await self._clear_referrer(browser._page)

            # Navigate
            page = await browser._browser.get(url)
            simulator.update_page(page)
            browser._page = page
            result["pages_visited"] += 1

            start_time  = time.monotonic()
            is_returning = random.random() < self._returning_ratio
            result["is_returning"] = is_returning

            if is_returning:
                pattern = random.choice(self.RETURNING_PATTERNS)
                result["pattern"] = pattern
                await self._returning_behavior(
                    browser._page, simulator, pattern
                )
            else:
                result["pattern"] = "new_visitor"
                word_count = await simulator.get_word_count()
                await simulator.simulate_page_read(
                    content_type    = "homepage",
                    word_count      = word_count,
                    scroll          = True,
                    interact        = True,
                )

            # Multi-page (30%)
            if random.random() < 0.30:
                extra = await self._navigate_multiple(
                    browser, simulator, url
                )
                result["pages_visited"] += extra

            result["time_on_site"]  = time.monotonic() - start_time
            result["success"]       = True
            self._total_visits  += 1
            self._total_success += 1

            await self._event_bus.publish_simple(
                EventCategory.TRAFFIC_VISIT_COMPLETE,
                {
                    "type":         "direct",
                    "url":          url,
                    "is_returning": is_returning,
                    "time_on_site": round(result["time_on_site"], 2),
                },
                session_id=session.session_id,
            )

        except Exception as exc:
            result["error"] = str(exc)
            logger.error(f"[DirectTrafficEngine] Error: {exc}")

        return result

    async def _clear_referrer(self, page: Any) -> None:
        """Clear referrer for direct traffic."""
        try:
            await page.send(
                uc.cdp.network.set_extra_http_headers(
                    headers=uc.cdp.network.Headers({})
                )
            )
        except Exception:
            pass

    async def _returning_behavior(
        self,
        page:       Any,
        simulator:  Any,
        pattern:    str,
    ) -> None:
        """Simulate returning visitor patterns."""
        if pattern == "fast_scroll":
            await asyncio.sleep(random.uniform(0.5, 1.0))
            await simulator.scroll.scroll_to_depth(
                random.uniform(0.3, 0.7)
            )
            await asyncio.sleep(random.uniform(10, 30))

        elif pattern == "direct_click":
            await asyncio.sleep(random.uniform(0.3, 0.8))
            try:
                nav_links = await page.select_all(
                    "nav a, header a, .navigation a"
                )
                if nav_links:
                    link = random.choice(nav_links[:5])
                    await simulator.simulate_link_click(link)
                    await asyncio.sleep(random.uniform(15, 40))
            except Exception:
                await asyncio.sleep(random.uniform(10, 25))

        elif pattern == "navigation_bar":
            await asyncio.sleep(random.uniform(0.5, 1.5))
            try:
                menu = await page.select("nav, .navbar, header")
                if menu:
                    await simulator.mouse.move_to_element(menu)
                    await asyncio.sleep(random.uniform(1.0, 3.0))
            except Exception:
                pass
            await asyncio.sleep(random.uniform(15, 35))

        elif pattern == "search_internal":
            await asyncio.sleep(random.uniform(0.5, 1.0))
            try:
                search = await page.select(
                    "input[type='search'], input[name='s'], "
                    "input[placeholder*='search']"
                )
                if search:
                    terms = ["product", "information", "help"]
                    await simulator.keyboard.type_text(
                        random.choice(terms),
                        search,
                    )
                    await asyncio.sleep(random.uniform(2, 5))
            except Exception:
                pass

        elif pattern == "footer_check":
            await asyncio.sleep(random.uniform(0.5, 1.0))
            await simulator.scroll.scroll_to_depth(0.95)
            await asyncio.sleep(random.uniform(5, 15))
            await simulator.scroll.scroll_to_depth(0.1)
            await asyncio.sleep(random.uniform(5, 15))

    async def _navigate_multiple(
        self,
        browser:    Any,
        simulator:  Any,
        base_url:   str,
        max_pages:  int = 3,
    ) -> int:
        """Navigate to multiple internal pages."""
        domain  = urlparse(base_url).netloc
        added   = 0

        for _ in range(random.randint(1, max_pages)):
            try:
                links = await browser._page.select_all("a")
                internal = [
                    l for l in links
                    if domain in (l.attrs.get("href", ""))
                ]
                if not internal:
                    break

                link = random.choice(internal[:8])
                await simulator.simulate_link_click(link)
                simulator.update_page(browser._page)
                await asyncio.sleep(random.uniform(0.5, 1.5))

                word_count = await simulator.get_word_count()
                await simulator.simulate_page_read(
                    content_type    = "article",
                    word_count      = word_count,
                    scroll          = True,
                    interact        = False,
                )
                added += 1

            except Exception:
                break

        return added

    def get_metrics(self) -> Dict[str, Any]:
        return {
            "total_visits":  self._total_visits,
            "total_success": self._total_success,
        }