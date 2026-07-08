"""
Jubra Traffic Pro - CDP Injector (nodriver Edition)
Native CDP communication without chromedriver.
"""

import asyncio
import json
import logging
import time
from typing import Any, Dict, List, Optional

try:
    import nodriver as uc
    HAS_NODRIVER = True
except ImportError:
    HAS_NODRIVER = False

logger = logging.getLogger(__name__)


class CDPInjector:
    """
    nodriver Native CDP Injector.

    Uses nodriver's direct CDP access:
    ─────────────────────────────────────────────────────
    • page.send(cdp.xxx) for all CDP commands
    • No selenium dependency
    • Faster and more reliable
    • Native async support
    """

    def __init__(self, page: Any):
        """
        Args:
            page: nodriver Tab/Page object
        """
        self._page          = page
        self._scripts:      Dict[str, str]  = {}
        self._inject_count: int             = 0
        self._fail_count:   int             = 0

    # ── Script Injection ───────────────────────────────────

    async def inject_script(
        self,
        name:   str,
        source: str,
    ) -> bool:
        """Inject script to run on every new document."""
        try:
            # nodriver native CDP
            await self._page.send(
                uc.cdp.page.add_script_to_evaluate_on_new_document(
                    source=source,
                )
            )
            # Execute immediately on current page
            await self._page.evaluate(source)

            self._scripts[name] = source
            self._inject_count += 1
            logger.debug(f"[CDPInjector] Injected: {name}")
            return True

        except Exception as exc:
            self._fail_count += 1
            logger.error(f"[CDPInjector] Inject error '{name}': {exc}")
            return False

    async def inject_all(self, scripts: Dict[str, str]) -> int:
        """Inject multiple scripts. Returns success count."""
        success = 0
        for name, source in scripts.items():
            if await self.inject_script(name, source):
                success += 1
        return success

    async def execute_now(self, source: str) -> Any:
        """Execute script immediately on current page."""
        try:
            return await self._page.evaluate(source)
        except Exception as exc:
            logger.debug(f"[CDPInjector] Execute error: {exc}")
            return None

    # ── Network Control ────────────────────────────────────

    async def enable_network(self) -> bool:
        """Enable CDP Network domain."""
        try:
            await self._page.send(uc.cdp.network.enable())
            return True
        except Exception as exc:
            logger.debug(f"[CDPInjector] Network enable error: {exc}")
            return False

    async def set_extra_headers(
        self,
        headers: Dict[str, str],
    ) -> bool:
        """Set extra HTTP request headers."""
        try:
            await self._page.send(
                uc.cdp.network.set_extra_http_headers(
                    headers=uc.cdp.network.Headers(headers)
                )
            )
            return True
        except Exception as exc:
            logger.debug(f"[CDPInjector] Headers error: {exc}")
            return False

    async def set_user_agent(
        self,
        user_agent:     str,
        accept_language: str    = "en-US,en;q=0.9",
        platform:       str     = "Win32",
    ) -> bool:
        """Override user agent via CDP."""
        try:
            await self._page.send(
                uc.cdp.network.set_user_agent_override(
                    user_agent=user_agent,
                    accept_language=accept_language,
                    platform=platform,
                )
            )
            return True
        except Exception as exc:
            logger.debug(f"[CDPInjector] UA override error: {exc}")
            return False

    async def block_urls(self, patterns: List[str]) -> bool:
        """Block specific URL patterns."""
        try:
            await self._page.send(
                uc.cdp.network.set_blocked_ur_ls(
                    urls=patterns
                )
            )
            return True
        except Exception as exc:
            logger.debug(f"[CDPInjector] Block URLs error: {exc}")
            return False

    # ── Cookie Management ──────────────────────────────────

    async def set_cookies(
        self,
        cookies: List[Dict[str, Any]],
    ) -> int:
        """Set cookies via CDP."""
        set_count = 0
        for cookie in cookies:
            try:
                await self._page.send(
                    uc.cdp.network.set_cookie(
                        name    = cookie.get("name", ""),
                        value   = cookie.get("value", ""),
                        domain  = cookie.get("domain", ""),
                        path    = cookie.get("path", "/"),
                        secure  = cookie.get("secure", False),
                        http_only = cookie.get("httpOnly", False),
                    )
                )
                set_count += 1
            except Exception as exc:
                logger.debug(f"[CDPInjector] Cookie error: {exc}")
        return set_count

    async def clear_cookies(self) -> bool:
        """Clear all cookies."""
        try:
            await self._page.send(
                uc.cdp.network.clear_browser_cookies()
            )
            return True
        except Exception as exc:
            logger.debug(f"[CDPInjector] Clear cookies error: {exc}")
            return False

    async def get_cookies(self) -> List[Dict[str, Any]]:
        """Get all cookies."""
        try:
            cookies = await self._page.send(
                uc.cdp.network.get_all_cookies()
            )
            return [
                {
                    "name":     c.name,
                    "value":    c.value,
                    "domain":   c.domain,
                    "path":     c.path,
                }
                for c in cookies
            ]
        except Exception:
            return []

    # ── Emulation ──────────────────────────────────────────

    async def set_device_metrics(
        self,
        width:          int,
        height:         int,
        pixel_ratio:    float   = 1.0,
        mobile:         bool    = False,
    ) -> bool:
        """Set device metrics (viewport)."""
        try:
            await self._page.send(
                uc.cdp.emulation.set_device_metrics_override(
                    width                   = width,
                    height                  = height,
                    device_scale_factor     = pixel_ratio,
                    mobile                  = mobile,
                )
            )
            return True
        except Exception as exc:
            logger.debug(f"[CDPInjector] Device metrics error: {exc}")
            return False

    async def set_geolocation(
        self,
        latitude:   float,
        longitude:  float,
        accuracy:   float = 10.0,
    ) -> bool:
        """Set geolocation override."""
        try:
            await self._page.send(
                uc.cdp.emulation.set_geolocation_override(
                    latitude    = latitude,
                    longitude   = longitude,
                    accuracy    = accuracy,
                )
            )
            return True
        except Exception as exc:
            logger.debug(f"[CDPInjector] Geolocation error: {exc}")
            return False

    async def set_timezone(self, timezone_id: str) -> bool:
        """Override timezone."""
        try:
            await self._page.send(
                uc.cdp.emulation.set_timezone_override(
                    timezone_id=timezone_id,
                )
            )
            return True
        except Exception as exc:
            logger.debug(f"[CDPInjector] Timezone error: {exc}")
            return False

    async def set_locale(self, locale: str) -> bool:
        """Override locale."""
        try:
            await self._page.send(
                uc.cdp.emulation.set_locale_override(
                    locale=locale,
                )
            )
            return True
        except Exception as exc:
            logger.debug(f"[CDPInjector] Locale error: {exc}")
            return False

    async def enable_touch(self, max_points: int = 5) -> bool:
        """Enable touch emulation."""
        try:
            await self._page.send(
                uc.cdp.emulation.set_touch_emulation_enabled(
                    enabled         = True,
                    max_touch_points = max_points,
                )
            )
            return True
        except Exception as exc:
            logger.debug(f"[CDPInjector] Touch error: {exc}")
            return False

    # ── Performance ────────────────────────────────────────

    async def get_performance_metrics(self) -> Dict[str, float]:
        """Get CDP performance metrics."""
        try:
            await self._page.send(uc.cdp.performance.enable())
            result = await self._page.send(
                uc.cdp.performance.get_metrics()
            )
            return {m.name: m.value for m in result}
        except Exception:
            return {}

    async def take_screenshot(
        self,
        quality: int = 80,
    ) -> Optional[bytes]:
        """Take screenshot via CDP."""
        try:
            import base64
            result = await self._page.send(
                uc.cdp.page.capture_screenshot(
                    format="jpeg",
                    quality=quality,
                )
            )
            if result:
                return base64.b64decode(result)
        except Exception as exc:
            logger.debug(f"[CDPInjector] Screenshot error: {exc}")
        return None

    # ── localStorage ───────────────────────────────────────

    async def set_local_storage(
        self,
        data: Dict[str, str],
    ) -> bool:
        """Set localStorage values via JS."""
        if not data:
            return True
        try:
            script = "\n".join(
                f"localStorage.setItem({json.dumps(k)}, {json.dumps(v)});"
                for k, v in data.items()
            )
            await self._page.evaluate(script)
            return True
        except Exception as exc:
            logger.debug(f"[CDPInjector] LocalStorage error: {exc}")
            return False

    async def get_local_storage(self, key: str) -> Optional[str]:
        """Get localStorage value."""
        try:
            result = await self._page.evaluate(
                f"localStorage.getItem({json.dumps(key)});"
            )
            return result
        except Exception:
            return None

    # ── Stats ──────────────────────────────────────────────

    def get_stats(self) -> Dict[str, Any]:
        return {
            "engine":           "nodriver",
            "registered":       len(self._scripts),
            "inject_count":     self._inject_count,
            "fail_count":       self._fail_count,
        }