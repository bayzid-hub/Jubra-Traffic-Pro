"""
Jubra Traffic Pro - AI Navigator (nodriver Edition)
Intelligent page navigation using nodriver's native element selection.
"""

import asyncio
import random
import logging
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

try:
    import nodriver as uc
    HAS_NODRIVER = True
except ImportError:
    HAS_NODRIVER = False

from core.config_manager import ConfigManager

logger = logging.getLogger(__name__)


class AINavigator:
    """
    nodriver AI Navigator.

    Key changes:
    ─────────────────────────────────────────────────────
    • page.select_all("a") instead of By.TAG_NAME
    • element.attrs["href"] instead of get_attribute()
    • page.back() for back navigation
    • simulator.update_page() after every navigation
    """

    GOALS = {
        "explore":      "Browse generally",
        "find_product": "Find product page",
        "find_contact": "Find contact info",
        "find_pricing": "Find pricing",
        "find_about":   "Find about page",
        "conversion":   "Complete conversion",
    }

    GOAL_KEYWORDS = {
        "find_product": ["product", "buy", "shop", "item", "service"],
        "find_contact": ["contact", "reach", "support", "email"],
        "find_pricing": ["pricing", "plans", "price", "cost"],
        "find_about":   ["about", "team", "company", "story"],
        "conversion":   ["sign up", "register", "get started", "trial"],
        "explore":      [],
    }

    GOAL_INDICATORS = {
        "find_product":  ["product", "shop", "item"],
        "find_contact":  ["contact", "support"],
        "find_pricing":  ["pricing", "plans"],
        "find_about":    ["about", "team"],
        "conversion":    ["thank", "success", "confirmed"],
    }

    def __init__(
        self,
        page:           Any,    # nodriver Tab
        base_url:       str     = "",
        goal:           str     = "explore",
        max_depth:      int     = 5,
        max_pages:      int     = 8,
    ):
        self._page          = page
        self._base_url      = base_url
        self._base_domain   = urlparse(base_url).netloc if base_url else ""
        self._goal          = goal
        self._max_depth     = max_depth
        self._max_pages     = max_pages
        self._visited:      set = set()
        self._nav_history:  List[str] = []
        self._current_depth: int = 0
        self._pages_visited: int = 0

    async def navigate_session(
        self,
        simulator:  Any,
        start_url:  str,
    ) -> Dict[str, Any]:
        """Execute goal-oriented navigation session."""
        result = {
            "goal":          self._goal,
            "pages_visited": 0,
            "goal_reached":  False,
            "path":          [],
            "final_url":     "",
        }

        self._visited.add(start_url)
        self._nav_history.append(start_url)
        result["path"].append(start_url)

        while (
            self._pages_visited < self._max_pages and
            self._current_depth < self._max_depth
        ):
            # Get links using nodriver
            links = await self._get_links()
            if not links:
                break

            # Score links
            scored = self._score_links(links)
            if not scored:
                break

            next_url, score = scored[0]

            try:
                # Navigate via nodriver
                new_page = await self._page.get(next_url)
                if new_page:
                    self._page = new_page
                    simulator.update_page(new_page)

                await asyncio.sleep(random.uniform(1.0, 2.5))

                self._visited.add(next_url)
                self._nav_history.append(next_url)
                self._pages_visited += 1
                self._current_depth += 1
                result["path"].append(next_url)

                # Quick page interaction
                await self._quick_interact(simulator)

                # Check goal
                current_url = self._page.url or ""
                if self._check_goal(current_url):
                    result["goal_reached"] = True
                    logger.info(
                        f"[AINavigator] Goal reached: {self._goal}"
                    )
                    break

            except Exception as exc:
                logger.debug(f"[AINavigator] Nav error: {exc}")
                break

        result["pages_visited"] = self._pages_visited
        result["final_url"]     = (
            self._nav_history[-1] if self._nav_history else ""
        )
        return result

    async def _get_links(self) -> List[Dict[str, str]]:
        """Get all valid internal links from current page."""
        try:
            # nodriver: select all anchor tags
            elements = await self._page.select_all("a")
            links    = []

            for el in elements[:50]:
                try:
                    href = el.attrs.get("href", "")
                    text = el.text or ""

                    if not href or href in self._visited:
                        continue
                    if not href.startswith("http"):
                        # Handle relative URLs
                        if href.startswith("/") and self._base_domain:
                            href = f"https://{self._base_domain}{href}"
                        else:
                            continue

                    parsed = urlparse(href)
                    if (
                        self._base_domain and
                        parsed.netloc != self._base_domain
                    ):
                        continue

                    links.append({
                        "url":     href,
                        "text":    text.strip(),
                        "element": el,
                    })
                except Exception:
                    continue

            return links

        except Exception as exc:
            logger.debug(f"[AINavigator] Get links error: {exc}")
            return []

    def _score_links(
        self,
        links: List[Dict[str, str]],
    ) -> List[Tuple[str, float]]:
        """Score links by goal relevance."""
        keywords    = self.GOAL_KEYWORDS.get(self._goal, [])
        scored      = []

        for link in links:
            score   = 0.5
            url     = link["url"].lower()
            text    = link["text"].lower()

            for kw in keywords:
                if kw in url or kw in text:
                    score += 0.3

            # Prefer shallower URLs
            url_depth = url.count("/") - 2
            score    -= url_depth * 0.05

            # Avoid recently visited
            if any(v in url for v in list(self._visited)[:5]):
                score -= 0.3

            # Random exploration
            score += random.uniform(0, 0.1)

            scored.append((link["url"], min(1.0, max(0.0, score))))

        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:5]

    def _check_goal(self, url: str) -> bool:
        """Check if current page satisfies goal."""
        url_lower   = url.lower()
        indicators  = self.GOAL_INDICATORS.get(self._goal, [])
        return any(ind in url_lower for ind in indicators)

    async def _quick_interact(self, simulator: Any) -> None:
        """Quick interaction on navigated page."""
        await asyncio.sleep(random.uniform(3, 12))
        await simulator.scroll.scroll_by(
            random.randint(150, 400)
        )
        await asyncio.sleep(random.uniform(2, 8))

    def update_page(self, page: Any) -> None:
        """Update page reference."""
        self._page = page

    def get_summary(self) -> Dict[str, Any]:
        return {
            "goal":          self._goal,
            "pages_visited": self._pages_visited,
            "nav_history":   self._nav_history,
            "visited_count": len(self._visited),
        }