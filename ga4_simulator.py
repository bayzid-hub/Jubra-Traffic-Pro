"""
Jubra Traffic Pro - Google Analytics 4 Simulator
Complete GA4 Measurement Protocol implementation with
event simulation, session tracking, and engagement modeling.
"""

import asyncio
import time
import uuid
import json
import random
import hashlib
import logging
import aiohttp
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
from collections import deque
from enum import Enum, auto
from urllib.parse import urlparse

from core.config_manager import ConfigManager
from core.event_bus import EventBus, EventCategory, get_event_bus

logger = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# GA4 Event Types
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class GA4EventType(Enum):
    """Standard GA4 event types."""
    PAGE_VIEW           = "page_view"
    SESSION_START       = "session_start"
    FIRST_VISIT         = "first_visit"
    USER_ENGAGEMENT     = "user_engagement"
    SCROLL              = "scroll"
    CLICK               = "click"
    VIEW_SEARCH_RESULTS = "view_search_results"
    SEARCH              = "search"
    SELECT_CONTENT      = "select_content"
    VIEW_ITEM           = "view_item"
    ADD_TO_CART         = "add_to_cart"
    PURCHASE            = "purchase"
    SIGN_UP             = "sign_up"
    LOGIN               = "login"
    SHARE               = "share"
    FILE_DOWNLOAD       = "file_download"
    VIDEO_START         = "video_start"
    VIDEO_PROGRESS      = "video_progress"
    VIDEO_COMPLETE      = "video_complete"
    FORM_START          = "form_start"
    FORM_SUBMIT         = "form_submit"
    GENERATE_LEAD       = "generate_lead"
    CUSTOM              = "custom_event"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# GA4 Client
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@dataclass
class GA4Client:
    """
    GA4 client identity for a session.
    Persists across events in same session.
    """
    client_id:          str
    session_id:         str
    measurement_id:     str
    api_secret:         str
    user_id:            Optional[str]       = None
    is_new_user:        bool                = True
    session_number:     int                 = 1
    engagement_time_ms: int                 = 0
    session_start:      float               = field(
        default_factory=time.monotonic
    )
    events_sent:        int                 = 0
    page_views:         int                 = 0

    @property
    def session_duration_ms(self) -> int:
        return int((time.monotonic() - self.session_start) * 1000)

    def generate_client_id(self) -> str:
        """GA4 format: random.timestamp"""
        rand_part = random.randint(100000000, 999999999)
        ts_part   = int(time.time())
        return f"{rand_part}.{ts_part}"


@dataclass
class GA4Event:
    """A single GA4 event with all required parameters."""
    name:           str
    params:         Dict[str, Any]      = field(default_factory=dict)
    timestamp_ms:   int                 = field(
        default_factory=lambda: int(time.time() * 1000)
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name":         self.name,
            "params":       self.params,
            "timestamp_micros": self.timestamp_ms * 1000,
        }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# GA4 Simulator
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class GA4Simulator:
    """
    Jubra Traffic Pro - Google Analytics 4 Simulator

    Implements full GA4 Measurement Protocol:
    ─────────────────────────────────────────────────────
    • Real GA4 event schema with all required parameters
    • Proper session management (session_id, client_id)
    • Engagement time tracking
    • Page view with enhanced measurement
    • E-commerce event simulation
    • Scroll depth events (25/50/75/90/100%)
    • User property management
    • Campaign attribution (UTM parameters)
    • Debug/validation mode support
    • Batched event sending
    • Retry logic with exponential backoff
    """

    GA4_COLLECT_URL = "https://www.google-analytics.com/mp/collect"
    GA4_DEBUG_URL   = "https://www.google-analytics.com/debug/mp/collect"

    def __init__(
        self,
        config:             ConfigManager,
        event_bus:          Optional[EventBus]  = None,
        measurement_id:     str                 = "",
        api_secret:         str                 = "",
        debug_mode:         bool                = False,
        send_events:        bool                = True,
        max_retry:          int                 = 3,
    ):
        self._config        = config
        self._event_bus     = event_bus or get_event_bus()
        self._measurement_id = (
            measurement_id or
            config.get("analytics.ga4_measurement_id", "")
        )
        self._api_secret    = (
            api_secret or
            config.get("analytics.ga4_api_secret", "")
        )
        self._debug_mode    = debug_mode
        self._send_events   = send_events
        self._max_retry     = max_retry

        # Session storage
        self._clients:      Dict[str, GA4Client]    = {}
        self._event_queue:  Dict[str, List[GA4Event]] = {}

        # Metrics
        self._total_sent:   int     = 0
        self._total_failed: int     = 0
        self._total_events: int     = 0
        self._send_times:   deque   = deque(maxlen=200)

        logger.info(
            f"[GA4Simulator] Initialized: "
            f"measurement_id={self._measurement_id[:10]}..., "
            f"send={send_events}, debug={debug_mode}"
        )

    # ── Client Management ──────────────────────────────────

    def create_client(
        self,
        session_id:     str,
        measurement_id: Optional[str]   = None,
        user_agent:     str             = "",
        language:       str             = "en-US",
        is_new_user:    bool            = True,
    ) -> GA4Client:
        """Create a GA4 client for a new session."""
        client = GA4Client(
            client_id       = f"{random.randint(10**9,10**10-1)}.{int(time.time())}",
            session_id      = session_id,
            measurement_id  = measurement_id or self._measurement_id,
            api_secret      = self._api_secret,
            is_new_user     = is_new_user,
            session_number  = 1,
        )
        self._clients[session_id]    = client
        self._event_queue[session_id] = []
        return client

    def get_client(self, session_id: str) -> Optional[GA4Client]:
        return self._clients.get(session_id)

    def release_client(self, session_id: str) -> None:
        self._clients.pop(session_id, None)
        self._event_queue.pop(session_id, None)

    # ── Event Creation ─────────────────────────────────────

    async def send_page_view(
        self,
        session_id:     str,
        page_url:       str,
        page_title:     str         = "",
        referrer:       str         = "",
        engagement_ms:  int         = 0,
    ) -> bool:
        """Send a page_view event with full parameters."""
        client = self._clients.get(session_id)
        if not client:
            return False

        client.page_views += 1
        parsed = urlparse(page_url)

        params = {
            "page_location":    page_url,
            "page_title":       page_title or parsed.path,
            "page_referrer":    referrer,
            "page_hostname":    parsed.netloc,
            "page_path":        parsed.path,
            "session_id":       client.session_id,
            "engagement_time_msec": max(engagement_ms, 100),
            "page_num":         client.page_views,
        }

        if client.is_new_user and client.page_views == 1:
            # First visit sends both first_visit and session_start
            await self._queue_event(
                session_id, GA4EventType.FIRST_VISIT.value, {
                    "session_id": client.session_id,
                }
            )
            await self._queue_event(
                session_id, GA4EventType.SESSION_START.value, {
                    "session_id":       client.session_id,
                    "ga_session_number": client.session_number,
                }
            )

        return await self._queue_event(
            session_id, GA4EventType.PAGE_VIEW.value, params
        )

    async def send_scroll(
        self,
        session_id:     str,
        page_url:       str,
        scroll_depth:   int,    # Percentage: 25, 50, 75, 90, 100
        engagement_ms:  int     = 0,
    ) -> bool:
        """Send scroll depth event (25/50/75/90/100%)."""
        client = self._clients.get(session_id)
        if not client:
            return False

        # GA4 only tracks 90% scroll by default
        if scroll_depth not in (25, 50, 75, 90, 100):
            return False

        return await self._queue_event(
            session_id, GA4EventType.SCROLL.value, {
                "percent_scrolled": scroll_depth,
                "page_location":    page_url,
                "engagement_time_msec": max(engagement_ms, 1),
            }
        )

    async def send_user_engagement(
        self,
        session_id:     str,
        engagement_ms:  int,
        page_url:       str = "",
    ) -> bool:
        """Send user_engagement event for dwell time."""
        client = self._clients.get(session_id)
        if not client:
            return False

        client.engagement_time_ms += engagement_ms

        return await self._queue_event(
            session_id, GA4EventType.USER_ENGAGEMENT.value, {
                "engagement_time_msec": engagement_ms,
                "page_location":        page_url,
                "session_id":           client.session_id,
            }
        )

    async def send_search(
        self,
        session_id:     str,
        search_term:    str,
        page_url:       str = "",
    ) -> bool:
        """Send search event."""
        return await self._queue_event(
            session_id, GA4EventType.SEARCH.value, {
                "search_term":  search_term,
                "page_location": page_url,
            }
        )

    async def send_click(
        self,
        session_id:     str,
        link_url:       str,
        link_text:      str     = "",
        link_domain:    str     = "",
        outbound:       bool    = False,
    ) -> bool:
        """Send click event for outbound/internal links."""
        return await self._queue_event(
            session_id, GA4EventType.CLICK.value, {
                "link_url":     link_url,
                "link_text":    link_text,
                "link_domain":  link_domain or urlparse(link_url).netloc,
                "outbound":     outbound,
            }
        )

    async def send_view_item(
        self,
        session_id:     str,
        item_id:        str,
        item_name:      str,
        item_category:  str     = "",
        price:          float   = 0.0,
        currency:       str     = "USD",
    ) -> bool:
        """Send view_item e-commerce event."""
        return await self._queue_event(
            session_id, GA4EventType.VIEW_ITEM.value, {
                "currency": currency,
                "value":    price,
                "items": [{
                    "item_id":       item_id,
                    "item_name":     item_name,
                    "item_category": item_category,
                    "price":         price,
                    "quantity":      1,
                }],
            }
        )

    async def send_custom_event(
        self,
        session_id:     str,
        event_name:     str,
        params:         Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Send a custom GA4 event."""
        return await self._queue_event(
            session_id, event_name, params or {}
        )

    # ── Batch Sending ──────────────────────────────────────

    async def flush(self, session_id: str) -> int:
        """
        Send all queued events for a session.
        Returns count of successfully sent events.
        """
        events = self._event_queue.get(session_id, [])
        if not events:
            return 0

        client = self._clients.get(session_id)
        if not client:
            return 0

        sent = 0
        # GA4 allows max 25 events per request
        batch_size = 25
        for i in range(0, len(events), batch_size):
            batch  = events[i:i + batch_size]
            ok     = await self._send_batch(client, batch)
            if ok:
                sent += len(batch)

        self._event_queue[session_id] = []
        return sent

    async def simulate_full_session(
        self,
        session_id:     str,
        pages:          List[Dict[str, Any]],
        search_terms:   Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """
        Simulate a complete GA4 session with realistic events.

        Args:
            pages: List of dicts with keys: url, title, time_ms, scroll_pct
            search_terms: Optional search queries during session
        """
        client = self._clients.get(session_id)
        if not client:
            return {"success": False, "events": 0}

        total_events = 0

        for i, page in enumerate(pages):
            url         = page.get("url", "")
            title       = page.get("title", "")
            time_ms     = page.get("time_ms", 30000)
            scroll_pct  = page.get("scroll_pct", 50)
            referrer    = pages[i-1].get("url", "") if i > 0 else ""

            # Page view
            await self.send_page_view(
                session_id      = session_id,
                page_url        = url,
                page_title      = title,
                referrer        = referrer,
                engagement_ms   = min(time_ms // 3, 5000),
            )
            total_events += 1

            # Scroll events (send at appropriate depths)
            scroll_milestones = [25, 50, 75, 90]
            for milestone in scroll_milestones:
                if scroll_pct >= milestone:
                    await asyncio.sleep(
                        random.uniform(0.1, 0.5)
                    )
                    await self.send_scroll(
                        session_id   = session_id,
                        page_url     = url,
                        scroll_depth = milestone,
                        engagement_ms = int(
                            time_ms * (milestone / 100) * 0.8
                        ),
                    )
                    total_events += 1

            # Search events
            if search_terms and i == 0:
                for term in search_terms[:2]:
                    await self.send_search(
                        session_id  = session_id,
                        search_term = term,
                        page_url    = url,
                    )
                    total_events += 1

            # User engagement
            await self.send_user_engagement(
                session_id   = session_id,
                engagement_ms = time_ms,
                page_url     = url,
            )
            total_events += 1

            # Simulate reading time delay
            await asyncio.sleep(random.uniform(0.05, 0.2))

        # Flush all events
        sent = await self.flush(session_id)
        self._total_events += total_events

        return {
            "success":      True,
            "events_queued": total_events,
            "events_sent":  sent,
            "client_id":    client.client_id,
        }

    # ── HTTP Sending ───────────────────────────────────────

    async def _queue_event(
        self,
        session_id: str,
        event_name: str,
        params:     Dict[str, Any],
    ) -> bool:
        """Add event to the queue."""
        if session_id not in self._event_queue:
            self._event_queue[session_id] = []

        event = GA4Event(
            name   = event_name,
            params = params,
        )
        self._event_queue[session_id].append(event)
        return True

    async def _send_batch(
        self,
        client: GA4Client,
        events: List[GA4Event],
    ) -> bool:
        """Send a batch of events to GA4 Measurement Protocol."""
        if not self._send_events:
            # Simulate sending
            self._total_sent += len(events)
            client.events_sent += len(events)
            return True

        if not self._measurement_id or not self._api_secret:
            return True  # Skip if not configured

        url = self.GA4_DEBUG_URL if self._debug_mode else self.GA4_COLLECT_URL
        params = {
            "measurement_id": client.measurement_id,
            "api_secret":     client.api_secret,
        }

        payload = {
            "client_id":    client.client_id,
            "timestamp_micros": int(time.time() * 1_000_000),
            "user_properties": {
                "session_number": {"value": str(client.session_number)},
            },
            "events": [e.to_dict() for e in events],
        }

        if client.user_id:
            payload["user_id"] = client.user_id

        for attempt in range(self._max_retry):
            try:
                start = time.monotonic()
                async with aiohttp.ClientSession() as session:
                    async with session.post(
                        url,
                        params  = params,
                        json    = payload,
                        timeout = aiohttp.ClientTimeout(total=10),
                        headers = {
                            "Content-Type": "application/json",
                            "User-Agent":   (
                                "Mozilla/5.0 (compatible; GA4Simulator/6.0)"
                            ),
                        },
                    ) as resp:
                        elapsed = (time.monotonic() - start) * 1000
                        self._send_times.append(elapsed)

                        if resp.status in (200, 204, 400):
                            # 400 = debug validation result
                            self._total_sent += len(events)
                            client.events_sent += len(events)
                            return True

                        logger.debug(
                            f"[GA4Simulator] Send status: {resp.status}"
                        )

            except Exception as exc:
                if attempt < self._max_retry - 1:
                    await asyncio.sleep(2 ** attempt)
                else:
                    self._total_failed += len(events)
                    logger.error(
                        f"[GA4Simulator] Send failed: {exc}"
                    )

        return False

    # ── Metrics ────────────────────────────────────────────

    def get_metrics(self) -> Dict[str, Any]:
        avg_send = (
            sum(self._send_times) / len(self._send_times)
            if self._send_times else 0.0
        )
        return {
            "total_events":     self._total_events,
            "total_sent":       self._total_sent,
            "total_failed":     self._total_failed,
            "active_clients":   len(self._clients),
            "avg_send_ms":      round(avg_send, 2),
            "measurement_id":   self._measurement_id[:10] + "..."
            if self._measurement_id else "",
        }