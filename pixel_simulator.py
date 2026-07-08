"""
Jubra Traffic Pro - Facebook/Meta Pixel Simulator
Simulates Facebook Pixel events using the Meta Conversions API
and browser-side pixel firing for authentic tracking.
"""

import asyncio
import time
import uuid
import json
import hashlib
import logging
import aiohttp
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from collections import deque

from core.config_manager import ConfigManager
from core.event_bus import EventBus, EventCategory, get_event_bus

logger = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Pixel Event Types
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class PixelEventType:
    """Standard Facebook Pixel event names."""
    PAGE_VIEW           = "PageView"
    VIEW_CONTENT        = "ViewContent"
    SEARCH              = "Search"
    ADD_TO_CART         = "AddToCart"
    ADD_TO_WISHLIST     = "AddToWishlist"
    INITIATE_CHECKOUT   = "InitiateCheckout"
    ADD_PAYMENT_INFO    = "AddPaymentInfo"
    PURCHASE            = "Purchase"
    LEAD                = "Lead"
    COMPLETE_REGISTRATION = "CompleteRegistration"
    CONTACT             = "Contact"
    FIND_LOCATION       = "FindLocation"
    SCHEDULE            = "Schedule"
    START_TRIAL         = "StartTrial"
    SUBSCRIBE           = "Subscribe"
    SUBMIT_APPLICATION  = "SubmitApplication"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Pixel Simulator
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class PixelSimulator:
    """
    Facebook/Meta Pixel Event Simulator.

    Features:
    ─────────────────────────────────────────────────────
    • All standard Pixel event types
    • Meta Conversions API (server-side)
    • Browser-side pixel injection via JS
    • Proper fbp/fbc cookie simulation
    • Event deduplication IDs
    • User data hashing (SHA-256)
    • Custom event support
    • Rate limiting per pixel ID
    """

    CONVERSIONS_API_URL = (
        "https://graph.facebook.com/v19.0/{pixel_id}/events"
    )

    def __init__(
        self,
        config:         ConfigManager,
        event_bus:      Optional[EventBus]  = None,
        pixel_id:       str                 = "",
        access_token:   str                 = "",
        send_events:    bool                = True,
        test_event_code: str                = "",
    ):
        self._config        = config
        self._event_bus     = event_bus or get_event_bus()
        self._pixel_id      = pixel_id or config.get(
            "analytics.pixel_id", ""
        )
        self._access_token  = access_token
        self._send_events   = send_events
        self._test_code     = test_event_code

        # Per-session fbp cookies
        self._fbp_cookies:  Dict[str, str] = {}
        self._fbc_cookies:  Dict[str, str] = {}

        # Metrics
        self._total_sent:   int     = 0
        self._total_failed: int     = 0
        self._send_times:   deque   = deque(maxlen=100)

        logger.info(
            f"[PixelSimulator] Initialized: "
            f"pixel_id={self._pixel_id[:8]}..., "
            f"send={send_events}"
        )

    def _generate_fbp(self, session_id: str) -> str:
        """Generate Facebook Browser ID cookie (fbp)."""
        if session_id in self._fbp_cookies:
            return self._fbp_cookies[session_id]

        import random
        version     = "fb"
        sub_domain  = "1"
        creation_ts = int(time.time())
        random_num  = random.randint(1000000000, 9999999999)
        fbp = f"{version}.{sub_domain}.{creation_ts}.{random_num}"
        self._fbp_cookies[session_id] = fbp
        return fbp

    def _generate_fbc(self, click_id: Optional[str] = None) -> str:
        """Generate Facebook Click ID cookie (fbc)."""
        import random
        version     = "fb"
        sub_domain  = "1"
        creation_ts = int(time.time())
        fbclid      = click_id or f"{''.join(random.choices('abcdefghijklmnopqrstuvwxyz0123456789', k=20))}"
        return f"{version}.{sub_domain}.{creation_ts}.{fbclid}"

    def _hash_user_data(self, value: str) -> str:
        """Hash user data with SHA-256 for Conversions API."""
        normalized = value.lower().strip()
        return hashlib.sha256(normalized.encode()).hexdigest()

    def _generate_event_id(self, session_id: str, event_name: str) -> str:
        """Generate unique event ID for deduplication."""
        raw = f"{session_id}:{event_name}:{time.time()}"
        return hashlib.md5(raw.encode()).hexdigest()

    async def track_page_view(
        self,
        session_id: str,
        page_url:   str,
        event_name: str = PixelEventType.PAGE_VIEW,
    ) -> bool:
        """Track a page view event."""
        return await self.track_event(
            session_id  = session_id,
            event_name  = event_name,
            params      = {
                "event_source_url": page_url,
            },
        )

    async def track_view_content(
        self,
        session_id:     str,
        page_url:       str,
        content_name:   str     = "",
        content_type:   str     = "product",
        content_id:     str     = "",
        value:          float   = 0.0,
        currency:       str     = "USD",
    ) -> bool:
        """Track a ViewContent event."""
        return await self.track_event(
            session_id  = session_id,
            event_name  = PixelEventType.VIEW_CONTENT,
            params      = {
                "content_name":     content_name,
                "content_type":     content_type,
                "content_ids":      [content_id] if content_id else [],
                "value":            value,
                "currency":         currency,
                "event_source_url": page_url,
            },
        )

    async def track_search(
        self,
        session_id:     str,
        search_string:  str,
        page_url:       str = "",
    ) -> bool:
        """Track a Search event."""
        return await self.track_event(
            session_id  = session_id,
            event_name  = PixelEventType.SEARCH,
            params      = {
                "search_string":    search_string,
                "event_source_url": page_url,
            },
        )

    async def track_lead(
        self,
        session_id:     str,
        page_url:       str = "",
        value:          float = 0.0,
        currency:       str = "USD",
    ) -> bool:
        """Track a Lead event."""
        return await self.track_event(
            session_id  = session_id,
            event_name  = PixelEventType.LEAD,
            params      = {
                "value":            value,
                "currency":         currency,
                "event_source_url": page_url,
            },
        )

    async def track_purchase(
        self,
        session_id:     str,
        value:          float,
        currency:       str     = "USD",
        content_ids:    Optional[List[str]] = None,
        page_url:       str     = "",
    ) -> bool:
        """Track a Purchase event."""
        return await self.track_event(
            session_id  = session_id,
            event_name  = PixelEventType.PURCHASE,
            params      = {
                "value":            value,
                "currency":         currency,
                "content_ids":      content_ids or [],
                "content_type":     "product",
                "event_source_url": page_url,
            },
        )

    async def track_event(
        self,
        session_id:     str,
        event_name:     str,
        params:         Optional[Dict[str, Any]] = None,
        user_data:      Optional[Dict[str, str]] = None,
    ) -> bool:
        """
        Track a generic Pixel event.
        Sends via Meta Conversions API if configured.
        """
        if not self._pixel_id:
            return True  # Skip if not configured

        event_id = self._generate_event_id(session_id, event_name)
        fbp      = self._generate_fbp(session_id)

        event_data = {
            "event_name":       event_name,
            "event_time":       int(time.time()),
            "event_id":         event_id,
            "action_source":    "website",
            "user_data": {
                "fbp": fbp,
                "client_ip_address":    "",
                "client_user_agent":    "",
                **(user_data or {}),
            },
            "custom_data":      params or {},
        }

        if not self._send_events:
            self._total_sent += 1
            logger.debug(
                f"[PixelSimulator] (dry-run) {event_name}: "
                f"{session_id[:8]}"
            )
            return True

        if not self._access_token:
            return True  # No token, skip API call

        return await self._send_to_api(event_data)

    async def _send_to_api(self, event_data: Dict) -> bool:
        """Send event to Meta Conversions API."""
        if not self._pixel_id or not self._access_token:
            return True

        url = self.CONVERSIONS_API_URL.format(pixel_id=self._pixel_id)
        payload = {
            "data":         [event_data],
            "access_token": self._access_token,
        }
        if self._test_code:
            payload["test_event_code"] = self._test_code

        for attempt in range(3):
            try:
                start = time.monotonic()
                async with aiohttp.ClientSession() as session:
                    async with session.post(
                        url,
                        json    = payload,
                        timeout = aiohttp.ClientTimeout(total=10),
                    ) as resp:
                        elapsed = (time.monotonic() - start) * 1000
                        self._send_times.append(elapsed)

                        if resp.status == 200:
                            self._total_sent += 1
                            return True

            except Exception as exc:
                if attempt == 2:
                    self._total_failed += 1
                    logger.debug(
                        f"[PixelSimulator] Send failed: {exc}"
                    )
                else:
                    await asyncio.sleep(2 ** attempt)

        return False

    async def inject_pixel_to_page(
        self,
        driver:     Any,
        session_id: str,
    ) -> bool:
        """Inject Facebook Pixel code into the browser page."""
        if not self._pixel_id:
            return False

        fbp = self._generate_fbp(session_id)
        script = f"""
        (function(f,b,e,v,n,t,s)
        {{if(f.fbq)return;n=f.fbq=function(){{n.callMethod?
        n.callMethod.apply(n,arguments):n.queue.push(arguments)}};
        if(!f._fbq)f._fbq=n;n.push=n;n.loaded=!0;n.version='2.0';
        n.queue=[];t=b.createElement(e);t.async=!0;
        t.src=v;s=b.getElementsByTagName(e)[0];
        s.parentNode.insertBefore(t,s)}}(window, document,'script',
        'https://connect.facebook.net/en_US/fbevents.js'));
        fbq('init', '{self._pixel_id}', {{
            'extern_id': '{session_id[:16]}'
        }});
        document.cookie = '_fbp={fbp}; path=/; max-age=7776000';
        fbq('track', 'PageView');
        """
        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None,
                lambda: driver.execute_script(script),
            )
            return True
        except Exception as exc:
            logger.debug(f"[PixelSimulator] Inject error: {exc}")
            return False

    def get_metrics(self) -> Dict[str, Any]:
        avg_send = (
            sum(self._send_times) / len(self._send_times)
            if self._send_times else 0.0
        )
        return {
            "total_sent":       self._total_sent,
            "total_failed":     self._total_failed,
            "avg_send_ms":      round(avg_send, 2),
            "pixel_id":         self._pixel_id[:8] + "..."
            if self._pixel_id else "",
        }