"""
Jubra Traffic Pro - Analytics Blender
Master coordinator for all analytics platforms.
Ensures consistent data across GA4, Pixel, and Heatmap simulators.
"""

import asyncio
import time
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from analytics.ga4_simulator import GA4Simulator
from analytics.pixel_simulator import PixelSimulator
from analytics.heatmap_simulator import HeatmapSimulator
from core.event_bus import EventBus, EventCategory, get_event_bus
from core.config_manager import ConfigManager

logger = logging.getLogger(__name__)


@dataclass
class PageVisitData:
    """Unified page visit data for all analytics platforms."""
    session_id:         str
    page_url:           str
    page_title:         str             = ""
    referrer:           str             = ""
    time_on_page_ms:    int             = 0
    scroll_depth_pct:   int             = 0
    clicks:             int             = 0
    engagement_ms:      int             = 0
    is_bounce:          bool            = False
    is_conversion:      bool            = False
    traffic_source:     str             = "organic"
    device_type:        str             = "desktop"
    country:            str             = "US"
    search_term:        Optional[str]   = None
    timestamp:          float           = field(default_factory=time.time)


class AnalyticsBlender:
    """
    Master Analytics Coordinator.

    Sends consistent data to all configured analytics platforms:
    ─────────────────────────────────────────────────────
    • Google Analytics 4 (GA4)
    • Facebook/Meta Pixel
    • Hotjar/Matomo Heatmap
    • Ensures data consistency across platforms
    • Handles platform-specific event schemas
    • Batches events for efficiency
    • Respects per-platform rate limits
    """

    def __init__(
        self,
        config:         ConfigManager,
        event_bus:      Optional[EventBus]  = None,
        ga4:            Optional[GA4Simulator]      = None,
        pixel:          Optional[PixelSimulator]    = None,
        heatmap:        Optional[HeatmapSimulator]  = None,
    ):
        self._config    = config
        self._event_bus = event_bus or get_event_bus()

        # Initialize platforms based on config
        self._ga4 = ga4 or (
            GA4Simulator(config, event_bus)
            if config.get("analytics.ga4_enabled", False)
            else None
        )
        self._pixel = pixel or (
            PixelSimulator(config, event_bus)
            if config.get("analytics.pixel_enabled", False)
            else None
        )
        self._heatmap = heatmap or (
            HeatmapSimulator(config, event_bus)
            if config.get("analytics.heatmap_enabled", False)
            else None
        )

        # Session tracking
        self._sessions: Dict[str, Dict[str, Any]] = {}

        # Metrics
        self._total_events_sent:    int = 0
        self._total_pages_tracked:  int = 0

        logger.info(
            f"[AnalyticsBlender] Initialized: "
            f"ga4={self._ga4 is not None}, "
            f"pixel={self._pixel is not None}, "
            f"heatmap={self._heatmap is not None}"
        )

    async def start_session(
        self,
        session_id:     str,
        user_agent:     str     = "",
        language:       str     = "en-US",
        is_new_user:    bool    = True,
    ) -> None:
        """Initialize analytics tracking for a new session."""
        self._sessions[session_id] = {
            "start_time":   time.monotonic(),
            "page_count":   0,
            "is_new_user":  is_new_user,
        }

        # Initialize GA4 client
        if self._ga4:
            self._ga4.create_client(
                session_id  = session_id,
                is_new_user = is_new_user,
            )

        # Initialize Pixel session
        if self._pixel:
            await self._pixel.track_page_view(
                session_id = session_id,
                page_url   = "",
                event_name = "PageView",
            )

        logger.debug(
            f"[AnalyticsBlender] Session started: {session_id[:8]}"
        )

    async def track_page_view(self, data: PageVisitData) -> None:
        """
        Track a page view across all active analytics platforms.
        Ensures consistent data representation.
        """
        session = self._sessions.get(data.session_id, {})
        session["page_count"] = session.get("page_count", 0) + 1
        self._sessions[data.session_id] = session

        tasks = []

        # ── GA4 ───────────────────────────────────────────
        if self._ga4:
            ga4_client = self._ga4.get_client(data.session_id)
            if not ga4_client:
                self._ga4.create_client(data.session_id)

            tasks.append(
                self._ga4.send_page_view(
                    session_id      = data.session_id,
                    page_url        = data.page_url,
                    page_title      = data.page_title,
                    referrer        = data.referrer,
                    engagement_ms   = data.engagement_ms,
                )
            )

            # Scroll events
            for threshold in [25, 50, 75, 90]:
                if data.scroll_depth_pct >= threshold:
                    tasks.append(
                        self._ga4.send_scroll(
                            session_id      = data.session_id,
                            page_url        = data.page_url,
                            scroll_depth    = threshold,
                            engagement_ms   = data.engagement_ms,
                        )
                    )

            # Search event
            if data.search_term:
                tasks.append(
                    self._ga4.send_search(
                        session_id  = data.session_id,
                        search_term = data.search_term,
                        page_url    = data.page_url,
                    )
                )

            # User engagement
            if data.time_on_page_ms > 1000:
                tasks.append(
                    self._ga4.send_user_engagement(
                        session_id      = data.session_id,
                        engagement_ms   = data.time_on_page_ms,
                        page_url        = data.page_url,
                    )
                )

        # ── Facebook Pixel ────────────────────────────────
        if self._pixel:
            tasks.append(
                self._pixel.track_page_view(
                    session_id  = data.session_id,
                    page_url    = data.page_url,
                    event_name  = "PageView",
                )
            )

            if data.is_conversion:
                tasks.append(
                    self._pixel.track_event(
                        session_id  = data.session_id,
                        event_name  = "Lead",
                        params      = {
                            "content_name": data.page_title,
                            "value":        0,
                            "currency":     "USD",
                        },
                    )
                )

        # ── Heatmap ───────────────────────────────────────
        if self._heatmap:
            tasks.append(
                self._heatmap.record_page_view(
                    session_id      = data.session_id,
                    page_url        = data.page_url,
                    scroll_depth    = data.scroll_depth_pct,
                    time_on_page_s  = data.time_on_page_ms / 1000,
                )
            )

        # Execute all analytics in parallel
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        self._total_pages_tracked += 1

        logger.debug(
            f"[AnalyticsBlender] Tracked page: {data.page_url[:50]} | "
            f"platforms={len(tasks)}"
        )

    async def track_conversion(
        self,
        session_id:     str,
        page_url:       str,
        event_name:     str     = "Purchase",
        value:          float   = 0.0,
        currency:       str     = "USD",
        items:          Optional[List[Dict]] = None,
    ) -> None:
        """Track a conversion event across all platforms."""
        tasks = []

        if self._ga4:
            tasks.append(
                self._ga4.send_custom_event(
                    session_id  = session_id,
                    event_name  = "purchase" if event_name == "Purchase"
                                  else "generate_lead",
                    params      = {
                        "currency":     currency,
                        "value":        value,
                        "items":        items or [],
                        "page_location": page_url,
                    },
                )
            )

        if self._pixel:
            tasks.append(
                self._pixel.track_event(
                    session_id  = session_id,
                    event_name  = event_name,
                    params      = {
                        "value":    value,
                        "currency": currency,
                        "contents": items or [],
                    },
                )
            )

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        logger.info(
            f"[AnalyticsBlender] Conversion tracked: "
            f"{event_name}, value={value}"
        )

    async def end_session(self, session_id: str) -> None:
        """Flush and end analytics session."""
        tasks = []

        if self._ga4:
            tasks.append(self._ga4.flush(session_id))

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        if self._ga4:
            self._ga4.release_client(session_id)

        self._sessions.pop(session_id, None)

        logger.debug(
            f"[AnalyticsBlender] Session ended: {session_id[:8]}"
        )

    async def simulate_full_session(
        self,
        session_id:     str,
        pages:          List[Dict[str, Any]],
        traffic_source: str = "organic",
        device_type:    str = "desktop",
        country:        str = "US",
    ) -> Dict[str, Any]:
        """
        Simulate a complete multi-page analytics session.

        Args:
            pages: List of page dicts with url, title, time_ms, scroll_pct
        """
        await self.start_session(session_id)

        results = {
            "pages_tracked":    0,
            "events_sent":      0,
            "platforms_used":   [],
        }

        if self._ga4:
            results["platforms_used"].append("ga4")
        if self._pixel:
            results["platforms_used"].append("pixel")
        if self._heatmap:
            results["platforms_used"].append("heatmap")

        for i, page in enumerate(pages):
            visit_data = PageVisitData(
                session_id      = session_id,
                page_url        = page.get("url", ""),
                page_title      = page.get("title", ""),
                referrer        = pages[i-1].get("url", "") if i > 0 else "",
                time_on_page_ms = page.get("time_ms", 30000),
                scroll_depth_pct = page.get("scroll_pct", 50),
                engagement_ms   = page.get("time_ms", 30000),
                traffic_source  = traffic_source,
                device_type     = device_type,
                country         = country,
                search_term     = page.get("search_term"),
            )
            await self.track_page_view(visit_data)
            results["pages_tracked"] += 1

            # Small delay between page tracking
            await asyncio.sleep(0.1)

        await self.end_session(session_id)

        return results

    def get_metrics(self) -> Dict[str, Any]:
        return {
            "total_events_sent":    self._total_events_sent,
            "total_pages_tracked":  self._total_pages_tracked,
            "active_sessions":      len(self._sessions),
            "platforms": {
                "ga4":      self._ga4 is not None,
                "pixel":    self._pixel is not None,
                "heatmap":  self._heatmap is not None,
            },
        }