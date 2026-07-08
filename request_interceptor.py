"""
Jubra Traffic Pro - Request Interceptor
Advanced network level interception for header manipulation,
resource blocking (ads/trackers), and timing randomization.
"""

import asyncio
import json
import logging
import random
from typing import Any, Dict, List, Optional, Set
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

class RequestInterceptor:
    """
    CDP-based Request Interceptor.
    Blocks trackers, injects headers, and randomizes request timing.
    """

    # Common trackers and ads domains to block for performance and stealth
    BLOCK_LIST = {
        "google-analytics.com", "doubleclick.net", "facebook.net",
        "googleadservices.com", "adservice.google", "analytics.google.com",
        "hotjar.com", "mouseflow.com", "mixpanel.com"
    }

    def __init__(self, driver: Any):
        self._driver = driver
        self._enabled = False
        self._custom_headers: Dict[str, str] = {}

    async def enable(self, block_resources: bool = True):
        """Enable CDP network interception."""
        try:
            # Page.enable and Network.enable are usually handled by UC, 
            # but we ensure Fetch domain for granular control
            self._driver.execute_cdp_cmd("Fetch.enable", {
                "patterns": [{"urlPattern": "*", "requestStage": "Request"}]
            })
            
            # Setup Event Listener for Request
            self._driver.add_cdp_listener("Fetch.requestPaused", self._handle_request)
            self._enabled = True
            logger.info("[RequestInterceptor] Network interception active")
        except Exception as e:
            logger.error(f"Failed to enable interceptor: {e}")

    async def _handle_request(self, event: Dict):
        """Callback for intercepted requests."""
        request_id = event["requestId"]
        url = event["request"]["url"]
        resource_type = event.get("resourceType", "Other")

        # 1. Block Trackers
        if any(domain in url for domain in self.BLOCK_LIST):
            await self._driver.execute_cdp_cmd("Fetch.failRequest", {
                "requestId": request_id,
                "errorReason": "Aborted"
            })
            return

        # 2. Randomize Timing (subtle 5-50ms delay to break fingerprinting)
        if random.random() < 0.1:
            await asyncio.sleep(random.uniform(0.005, 0.05))

        # 3. Continue Request with Modified Headers
        headers = event["request"].get("headers", {})
        headers.update(self._custom_headers)
        
        try:
            await self._driver.execute_cdp_cmd("Fetch.continueRequest", {
                "requestId": request_id,
                "headers": [{"name": k, "value": v} for k, v in headers.items()]
            })
        except Exception:
            pass # Request might have already closed

    def set_headers(self, headers: Dict[str, str]):
        self._custom_headers.update(headers)