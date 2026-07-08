"""
Jubra Traffic Pro - Organic Traffic Engine (nodriver Edition)
Complete search engine simulation using nodriver.
No Selenium dependency.
"""

import asyncio
import time
import random
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse, urlencode

try:
    import nodriver as uc
    HAS_NODRIVER = True
except ImportError:
    HAS_NODRIVER = False

from core.event_bus import EventBus, EventCategory, get_event_bus
from core.config_manager import ConfigManager

logger = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Search Engine Profiles
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@dataclass
class SearchEngineProfile:
    """Complete search engine profile."""
    name:               str
    base_url:           str
    search_path:        str
    query_param:        str
    search_box_selector: str
    results_selector:   str
    next_page_selector: str
    suggested_selector: str
    referrer:           str
    pagination_param:   str     = "start"
    results_per_page:   int     = 10

    def build_url(self, query: str, page: int = 1) -> str:
        params = {self.query_param: query}
        if page > 1:
            params[self.pagination_param] = str(
                (page - 1) * self.results_per_page
            )
        return (
            f"{self.base_url}{self.search_path}"
            f"?{urlencode(params)}"
        )


SEARCH_ENGINES = {
    "google": SearchEngineProfile(
        name                = "Google",
        base_url            = "https://www.google.com",
        search_path         = "/search",
        query_param         = "q",
        search_box_selector = "textarea[name='q'], input[name='q']",
        results_selector    = "div.g, div[data-hveid]",
        next_page_selector  = "a#pnnext",
        suggested_selector  = "ul[role='listbox'] li",
        referrer            = "https://www.google.com/",
    ),
    "bing": SearchEngineProfile(
        name                = "Bing",
        base_url            = "https://www.bing.com",
        search_path         = "/search",
        query_param         = "q",
        search_box_selector = "input[name='q'], #sb_form_q",
        results_selector    = "li.b_algo",
        next_page_selector  = "a.sb_pagN",
        suggested_selector  = ".sa_sg li",
        referrer            = "https://www.bing.com/",
        pagination_param    = "first",
    ),
    "duckduckgo": SearchEngineProfile(
        name                = "DuckDuckGo",
        base_url            = "https://duckduckgo.com",
        search_path         = "/",
        query_param         = "q",
        search_box_selector = "input[name='q'], #searchbox_input",
        results_selector    = "article[data-testid='result']",
        next_page_selector  = "button#more-results",
        suggested_selector  = ".acp li",
        referrer            = "https://duckduckgo.com/",
    ),
    "yahoo": SearchEngineProfile(
        name                = "Yahoo",
        base_url            = "https://search.yahoo.com",
        search_path         = "/search",
        query_param         = "p",
        search_box_selector = "input[name='p'], #yschsp",
        results_selector    = "div.algo-sr",
        next_page_selector  = "a.next",
        suggested_selector  = "#yui-ac-container li",
        referrer            = "https://search.yahoo.com/",
        pagination_param    = "b",
    ),
}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# SERP Result
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@dataclass
class SERPResult:
    """A single search engine result."""
    position:   int
    title:      str
    url:        str
    domain:     str
    is_target:  bool        = False
    element:    Any         = None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Keyword Manager
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class KeywordManager:
    """Manages keyword pool with usage tracking."""

    def __init__(
        self,
        keywords_file:  Optional[str]   = None,
        target_domain:  str             = "",
    ):
        self._keywords: List[Dict]  = []
        self._domain    = target_domain
        self._used:     List[str]   = []

        self._load_defaults()
        if keywords_file:
            self._load_file(keywords_file)

    def _load_defaults(self) -> None:
        defaults = [
            "information guide", "best service 2024",
            "how to guide", "top rated products",
            "review comparison", "buy online cheap",
            "professional service", "free trial",
            "expert tips", "complete tutorial",
        ]
        for kw in defaults:
            self._keywords.append({
                "text": kw, "weight": 1.0, "used": 0
            })

    def _load_file(self, filepath: str) -> None:
        import json
        from pathlib import Path
        path = Path(filepath)
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, str):
                        self._keywords.append({
                            "text": item, "weight": 1.0, "used": 0
                        })
                    elif isinstance(item, dict):
                        self._keywords.append({
                            "text":   item.get("text", ""),
                            "weight": item.get("weight", 1.0),
                            "used":   0,
                        })
            elif isinstance(data, dict):
                for cat, kws in data.items():
                    for kw in kws:
                        self._keywords.append({
                            "text": kw, "weight": 1.0, "used": 0
                        })
        except Exception as exc:
            logger.error(f"[KeywordManager] Load error: {exc}")

    def get(self) -> str:
        """Get a weighted random keyword."""
        if not self._keywords:
            return "information"

        # Prefer less-used keywords
        candidates = [
            k for k in self._keywords
            if k["text"] not in self._used[-10:]
        ] or self._keywords

        weights = [k["weight"] for k in candidates]
        total   = sum(weights)
        r       = random.uniform(0, total)
        cumul   = 0.0
        for kw, w in zip(candidates, weights):
            cumul += w
            if r <= cumul:
                kw["used"] += 1
                self._used.append(kw["text"])
                return kw["text"]

        selected = candidates[-1]
        selected["used"] += 1
        return selected["text"]

    @property
    def count(self) -> int:
        return len(self._keywords)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Organic Traffic Engine (nodriver)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class OrganicTrafficEngine:
    """
    nodriver Organic Traffic Engine.

    Key changes from Selenium version:
    ─────────────────────────────────────────────────────
    • browser.get() returns nodriver Tab
    • Element finding via page.select() / page.select_all()
    • No WebDriverWait / By.CSS_SELECTOR
    • simulator.update_page() called after every navigation
    • Direct element.click() for interactions
    • Natural search box interaction via keyboard dynamics
    """

    # CTR by SERP position
    POSITION_CTR = {
        1: 0.289, 2: 0.152, 3: 0.111,
        4: 0.077, 5: 0.058, 6: 0.044,
        7: 0.035, 8: 0.030, 9: 0.026,
        10: 0.023,
    }

    def __init__(
        self,
        config:             ConfigManager,
        event_bus:          Optional[EventBus]  = None,
        target_domain:      str                 = "",
        target_urls:        Optional[List[str]] = None,
        keywords_file:      Optional[str]       = None,
        search_engines:     Optional[List[str]] = None,
        engine_weights:     Optional[Dict[str, float]] = None,
        force_target_click: bool                = False,
    ):
        self._config            = config
        self._event_bus         = event_bus or get_event_bus()
        self._target_domain     = target_domain
        self._target_urls       = target_urls or []
        self._force_target      = force_target_click

        # Keywords
        self._keywords = KeywordManager(keywords_file, target_domain)

        # Engine weights
        self._engine_weights = engine_weights or {
            "google":       0.65,
            "bing":         0.20,
            "duckduckgo":   0.10,
            "yahoo":        0.05,
        }

        # Active engines
        active  = search_engines or list(self._engine_weights.keys())
        self._engines: List[SearchEngineProfile] = [
            SEARCH_ENGINES[name]
            for name in active
            if name in SEARCH_ENGINES
        ]
        if not self._engines:
            self._engines = [SEARCH_ENGINES["google"]]

        # Metrics
        self._total_searches:   int = 0
        self._total_clicks:     int = 0
        self._target_clicks:    int = 0
        self._failed:           int = 0

        logger.info(
            f"[OrganicTrafficEngine] Initialized (nodriver): "
            f"engines={[e.name for e in self._engines]}"
        )

    # ── Core Flow ──────────────────────────────────────────

    async def execute_search_visit(
        self,
        session:        Any,
        browser:        Any,        # BrowserInstance
        simulator:      Any,        # HumanSimulator
        keyword:        Optional[str]   = None,
        engine_name:    Optional[str]   = None,
    ) -> Dict[str, Any]:
        """Execute a complete organic search visit."""
        result = {
            "success":          False,
            "engine":           "",
            "keyword":          "",
            "serp_position":    0,
            "clicked_url":      "",
            "time_on_serp":     0.0,
            "time_on_target":   0.0,
            "pages_visited":    0,
            "error":            None,
        }

        try:
            # Select engine and keyword
            engine  = self._select_engine(engine_name)
            kw      = keyword or self._keywords.get()
            result["engine"]    = engine.name
            result["keyword"]   = kw

            # ── Step 1: Navigate to search engine ──────────
            logger.debug(
                f"[OrganicTrafficEngine] {engine.name} → '{kw}'"
            )

            # nodriver: browser._browser.get() returns Tab
            page = await browser._browser.get(engine.base_url)
            simulator.update_page(page)
            browser._page = page

            await asyncio.sleep(random.uniform(1.2, 3.0))

            # ── Step 2: Type in search box ─────────────────
            serp_start  = time.monotonic()
            typed_ok    = await self._type_search(
                engine, kw, simulator, page
            )
            if not typed_ok:
                result["error"] = "Search box interaction failed"
                self._failed += 1
                return result

            # ── Step 3: Wait for SERP ──────────────────────
            await asyncio.sleep(random.uniform(1.5, 3.5))
            result["pages_visited"] += 1

            # Update page reference after navigation
            simulator.update_page(browser._page)

            # ── Step 4: Parse SERP results ─────────────────
            serp_results = await self._parse_serp(
                browser._page, engine
            )

            # Browse SERP naturally
            await self._browse_serp(
                simulator, browser._page, serp_results
            )

            # ── Step 5: Select result ──────────────────────
            selected = self._select_result(serp_results)
            result["time_on_serp"] = time.monotonic() - serp_start

            if not selected:
                result["error"] = "No suitable result"
                self._failed += 1
                return result

            result["serp_position"] = selected.position
            result["clicked_url"]   = selected.url

            # ── Step 6: Click result ───────────────────────
            clicked = await self._click_result(
                selected, simulator, browser._page
            )
            if not clicked:
                result["error"] = "Click failed"
                return result

            self._total_clicks += 1
            if selected.is_target:
                self._target_clicks += 1

            # Wait for target page
            await asyncio.sleep(random.uniform(1.5, 3.0))
            result["pages_visited"] += 1

            # Update simulator for new page
            simulator.update_page(browser._page)

            # ── Step 7: Read target page ───────────────────
            target_start    = time.monotonic()
            word_count      = await simulator.get_word_count()

            await simulator.simulate_page_read(
                content_type    = "article",
                word_count      = word_count,
                scroll          = True,
                interact        = True,
            )
            result["time_on_target"] = time.monotonic() - target_start

            # ── Step 8: Back to SERP (30% chance) ─────────
            if random.random() < 0.30:
                await asyncio.sleep(random.uniform(0.5, 2.0))
                # nodriver back navigation
                await browser._page.back()
                await asyncio.sleep(random.uniform(1.0, 2.5))
                result["pages_visited"] += 1
                simulator.update_page(browser._page)

            result["success"] = True
            self._total_searches += 1

            await self._event_bus.publish_simple(
                EventCategory.TRAFFIC_VISIT_COMPLETE,
                {
                    "type":     "organic",
                    "engine":   engine.name,
                    "keyword":  kw,
                    "position": selected.position,
                    "url":      selected.url,
                },
                session_id=session.session_id,
            )

            logger.info(
                f"[OrganicTrafficEngine] Complete: "
                f"engine={engine.name}, kw='{kw}', "
                f"pos={selected.position}"
            )

        except Exception as exc:
            result["error"] = str(exc)
            self._failed += 1
            logger.error(
                f"[OrganicTrafficEngine] Error: {exc}",
                exc_info=True,
            )

        return result

    # ── Search Box Interaction ─────────────────────────────

    async def _type_search(
        self,
        engine:     SearchEngineProfile,
        keyword:    str,
        simulator:  Any,
        page:       Any,
    ) -> bool:
        """Find search box and type query."""
        try:
            # Find search box
            search_box = None
            for selector in engine.search_box_selector.split(","):
                try:
                    search_box = await page.select(
                        selector.strip(),
                        timeout=8,
                    )
                    if search_box:
                        break
                except Exception:
                    continue

            if not search_box:
                return False

            # Move to search box
            await simulator.mouse.move_to_element(search_box)
            await asyncio.sleep(random.uniform(0.2, 0.5))

            # Click
            await search_box.click()
            await asyncio.sleep(random.uniform(0.3, 0.8))

            # Type keyword
            await simulator.keyboard.type_search_query(
                keyword, search_box
            )

            # Handle autocomplete (30% chance to interact)
            if random.random() < 0.30:
                await self._handle_autocomplete(
                    engine, simulator, page
                )

            # Press Enter
            await simulator.keyboard.press_key("ENTER")
            return True

        except Exception as exc:
            logger.debug(
                f"[OrganicTrafficEngine] Type search error: {exc}"
            )
            return False

    async def _handle_autocomplete(
        self,
        engine:     SearchEngineProfile,
        simulator:  Any,
        page:       Any,
    ) -> None:
        """Interact with autocomplete suggestions."""
        await asyncio.sleep(random.uniform(0.3, 0.8))
        try:
            suggestions = await page.select_all(
                engine.suggested_selector
            )
            if suggestions and len(suggestions) > 0:
                sugg = random.choice(suggestions[:4])
                await simulator.mouse.move_to_element(sugg)
                await asyncio.sleep(random.uniform(0.2, 0.6))
                # 20% chance to click suggestion
                if random.random() < 0.20:
                    await sugg.click()
        except Exception:
            pass

    # ── SERP Parsing ───────────────────────────────────────

    async def _parse_serp(
        self,
        page:   Any,
        engine: SearchEngineProfile,
    ) -> List[SERPResult]:
        """Parse SERP results from current page."""
        results: List[SERPResult] = []
        try:
            # Try each selector
            elements = []
            for selector in engine.results_selector.split(","):
                elements = await page.select_all(selector.strip())
                if elements:
                    break

            for i, el in enumerate(elements[:12]):
                try:
                    # Get title
                    title = ""
                    try:
                        title_el = await el.select("h3")
                        if title_el:
                            title = title_el.text or ""
                    except Exception:
                        pass

                    # Get URL
                    url = ""
                    try:
                        link_el = await el.select("a")
                        if link_el:
                            url = link_el.attrs.get("href", "")
                    except Exception:
                        pass

                    if not url or not url.startswith("http"):
                        continue

                    domain      = urlparse(url).netloc.lower()
                    is_target   = (
                        self._target_domain.replace("www.", "")
                        in domain.replace("www.", "")
                        if self._target_domain else False
                    )

                    results.append(SERPResult(
                        position    = i + 1,
                        title       = title.strip(),
                        url         = url,
                        domain      = domain,
                        is_target   = is_target,
                        element     = el,
                    ))

                except Exception:
                    continue

            logger.debug(
                f"[OrganicTrafficEngine] Parsed "
                f"{len(results)} results"
            )

        except Exception as exc:
            logger.debug(
                f"[OrganicTrafficEngine] Parse error: {exc}"
            )

        return results

    # ── SERP Browsing ──────────────────────────────────────

    async def _browse_serp(
        self,
        simulator:  Any,
        page:       Any,
        results:    List[SERPResult],
    ) -> None:
        """Browse SERP naturally before clicking."""
        # Initial scan
        await asyncio.sleep(random.uniform(1.5, 4.0))

        # Hover over first few results
        for result in results[:random.randint(2, 4)]:
            if result.element:
                try:
                    await simulator.mouse.move_to_element(
                        result.element
                    )
                    await asyncio.sleep(random.uniform(0.2, 0.9))
                except Exception:
                    pass

        # Scroll down to see more results
        n_scrolls = random.randint(1, 3)
        for _ in range(n_scrolls):
            await simulator.scroll.scroll_by(
                random.randint(200, 500)
            )
            await asyncio.sleep(random.uniform(0.5, 2.0))

        # Sometimes scroll back up
        if random.random() < 0.35:
            await simulator.scroll.scroll_by(
                -random.randint(100, 300)
            )
            await asyncio.sleep(random.uniform(0.5, 1.5))

    # ── Result Selection ───────────────────────────────────

    def _select_result(
        self,
        results: List[SERPResult],
    ) -> Optional[SERPResult]:
        """Select a result using CTR-weighted random."""
        if not results:
            return None

        # Force target if configured
        if self._force_target:
            targets = [r for r in results if r.is_target]
            if targets:
                return targets[0]

        # Target in results → 65% chance to click it
        target_results = [r for r in results if r.is_target]
        if target_results and random.random() < 0.65:
            return target_results[0]

        # CTR-weighted selection
        weights = [
            self.POSITION_CTR.get(r.position, 0.01) *
            (2.0 if r.is_target else 1.0)
            for r in results
        ]
        total   = sum(weights)
        if total <= 0:
            return random.choice(results)

        r = random.uniform(0, total)
        cumul = 0.0
        for result, weight in zip(results, weights):
            cumul += weight
            if r <= cumul:
                return result
        return results[0]

    async def _click_result(
        self,
        result:     SERPResult,
        simulator:  Any,
        page:       Any,
    ) -> bool:
        """Click a SERP result."""
        try:
            if result.element:
                # Move to element first
                await simulator.mouse.move_to_element(
                    result.element
                )
                await asyncio.sleep(random.uniform(0.3, 0.8))
                # Click via element
                await result.element.click()
                return True

            else:
                # Direct navigation fallback
                await page.get(result.url)
                return True

        except Exception as exc:
            logger.debug(
                f"[OrganicTrafficEngine] Click error: {exc}"
            )
            # Fallback: direct navigation
            try:
                await page.get(result.url)
                return True
            except Exception:
                return False

    # ── Engine Selection ───────────────────────────────────

    def _select_engine(
        self,
        name: Optional[str] = None,
    ) -> SearchEngineProfile:
        """Select engine by name or weighted random."""
        if name and name in SEARCH_ENGINES:
            return SEARCH_ENGINES[name]

        engines = list(self._engine_weights.keys())
        weights = [self._engine_weights[n] for n in engines]
        total   = sum(weights)
        r       = random.uniform(0, total)
        cumul   = 0.0
        for eng_name, w in zip(engines, weights):
            cumul += w
            if r <= cumul:
                profile = SEARCH_ENGINES.get(eng_name)
                if profile and profile in self._engines:
                    return profile

        return self._engines[0]

    def get_metrics(self) -> Dict[str, Any]:
        return {
            "total_searches":   self._total_searches,
            "total_clicks":     self._total_clicks,
            "target_clicks":    self._target_clicks,
            "failed":           self._failed,
            "keywords_pool":    self._keywords.count,
        }