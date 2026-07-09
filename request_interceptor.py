"""
Jubra Traffic Pro - Request Interceptor (nodriver Edition)
Advanced network level interception for header manipulation,
resource blocking (ads/trackers), and timing randomization.
"""

import asyncio
import json
import logging
import random
from typing import Any, Dict, List, Optional, Set
from dataclasses import dataclass, field

try:
    import nodriver as uc
    import nodriver.cdp.fetch as cdp_fetch
    import nodriver.cdp.network as cdp_network
    HAS_NODRIVER = True
except ImportError:
    HAS_NODRIVER = False

logger = logging.getLogger(__name__)


class RequestInterceptor:
    """
    nodriver CDP-based Request Interceptor.
    Blocks trackers, injects headers, and randomizes request timing.
    """

    # Common trackers and ads domains to block
    BLOCK_LIST = {
        "google-analytics.com", "doubleclick.net", "facebook.net",
        "googleadservices.com", "adservice.google", "analytics.google.com",
        "hotjar.com", "mouseflow.com", "mixpanel.com",
    }

    def __init__(self, page: Any = None):
        self._page = page
        self._enabled = False
        self._custom_headers: Dict[str, str] = {}

    def update_page(self, page: Any) -> None:
        """Update page reference after navigation."""
        self._page = page

    async def enable(self, block_resources: bool = True) -> None:
        """Enable CDP Fetch interception via nodriver."""
        if not HAS_NODRIVER or not self._page:
            logger.warning("[RequestInterceptor] nodriver or page not available")
            return

        try:
            await self._page.send(
                cdp_fetch.enable(
                    patterns=[
                        cdp_fetch.RequestPattern(
                            url_pattern="*",
                            request_stage=cdp_fetch.RequestStage.REQUEST,
                        )
                    ]
                )
            )

            self._page.add_handler(
                cdp_fetch.RequestPaused,
                self._handle_request,
            )

            self._enabled = True
            logger.info("[RequestInterceptor] Network interception active")

        except Exception as exc:
            logger.error(f"[RequestInterceptor] Failed to enable: {exc}")

    async def _handle_request(self, event: Any) -> None:
        """Callback for intercepted requests."""
        if not self._page:
            return

        request_id = event.request_id
        url = event.request.url if event.request else ""

        try:
            # 1. Block trackers
            if any(domain in url for domain in self.BLOCK_LIST):
                await self._page.send(
                    cdp_fetch.fail_request(
                        request_id=request_id,
                        error_reason=cdp_network.ErrorReason.ABORTED,
                    )
                )
                return

            # 2. Randomize timing
            if random.random() < 0.1:
                await asyncio.sleep(random.uniform(0.005, 0.05))

            # 3. Continue request with optional custom headers
            if self._custom_headers:
                headers = []
                if event.request and event.request.headers:
                    for name, value in event.request.headers.items():
                        headers.append(
                            cdp_fetch.HeaderEntry(name=name, value=value)
                        )
                for name, value in self._custom_headers.items():
                    headers.append(
                        cdp_fetch.HeaderEntry(name=name, value=value)
                    )

                await self._page.send(
                    cdp_fetch.continue_request(
                        request_id=request_id,
                        headers=headers,
                    )
                )
            else:
                await self._page.send(
                    cdp_fetch.continue_request(
                        request_id=request_id,
                    )
                )

        except Exception:
            try:
                await self._page.send(
                    cdp_fetch.continue_request(request_id=request_id)
                )
            except Exception:
                pass

    async def disable(self) -> None:
        """Disable Fetch interception."""
        if not self._page:
            return
        try:
            await self._page.send(cdp_fetch.disable())
            self._enabled = False
            logger.info("[RequestInterceptor] Interception disabled")
        except Exception as exc:
            logger.debug(f"[RequestInterceptor] Disable error: {exc}")

    def set_headers(self, headers: Dict[str, str]) -> None:
        """Set custom headers to inject into requests."""
        self._custom_headers.update(headers)

    @property
    def is_enabled(self) -> bool:
        return self._enabled
