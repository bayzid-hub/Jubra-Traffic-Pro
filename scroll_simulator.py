"""
Jubra Traffic Pro - Scroll Simulator (nodriver Edition)
Natural scroll behavior using nodriver CDP Input.dispatchMouseEvent
with wheel events - no Selenium dependency.
"""

import asyncio
import math
import random
import time
import logging
from typing import Any, Dict, Optional

try:
    import nodriver as uc
    HAS_NODRIVER = True
except ImportError:
    HAS_NODRIVER = False

logger = logging.getLogger(__name__)


class ScrollSimulator:
    """
    nodriver Scroll Simulator.

    Uses CDP Input.dispatchMouseEvent (mouseWheel):
    ─────────────────────────────────────────────────────
    • Real wheel events via CDP (not JS scroll)
    • Variable velocity and momentum
    • Natural reading pauses during scroll
    • Back-scroll (re-reading behavior)
    • Smooth scroll with acceleration/deceleration
    • Element-targeted scrolling
    """

    SCROLL_SPEEDS = {
        "slow":   {"delta": (40,  80),  "delay": (0.05, 0.12)},
        "normal": {"delta": (80,  160), "delay": (0.03, 0.08)},
        "fast":   {"delta": (150, 300), "delay": (0.02, 0.05)},
        "random": {"delta": (40,  300), "delay": (0.02, 0.12)},
    }

    def __init__(
        self,
        page:               Any,    # nodriver Tab
        speed_profile:      str     = "normal",
        back_scroll_rate:   float   = 0.20,
        reading_pause_rate: float   = 0.35,
        viewport_height:    int     = 1080,
    ):
        self._page              = page
        self._speed             = speed_profile
        self._back_scroll_rate  = back_scroll_rate
        self._reading_pause_rate = reading_pause_rate
        self._viewport_h        = viewport_height
        self._current_scroll    = 0
        self._max_scroll        = 0
        self._scroll_x          = 0.0
        self._scroll_y          = 0.0

    # ── CDP Wheel Event ────────────────────────────────────

    async def _dispatch_wheel(
        self,
        delta_y:    float,
        delta_x:    float   = 0.0,
        x:          float   = None,
        y:          float   = None,
    ) -> None:
        """Dispatch mouseWheel event via CDP."""
        # Default to center of viewport
        wheel_x = x if x is not None else 960.0
        wheel_y = y if y is not None else 540.0

        try:
            await self._page.send(
                uc.cdp.input_.dispatch_mouse_event(
                    type_       = "mouseWheel",
                    x           = wheel_x,
                    y           = wheel_y,
                    delta_x     = delta_x,
                    delta_y     = delta_y,
                    modifiers   = 0,
                )
            )
            # Update scroll tracking
            self._scroll_y = max(
                0,
                self._scroll_y + delta_y,
            )
        except Exception as exc:
            logger.debug(f"[ScrollSimulator] Wheel error: {exc}")

    # ── Page Dimensions ────────────────────────────────────

    async def _get_page_dimensions(self) -> Dict[str, int]:
        """Get page scroll dimensions."""
        try:
            dims = await self._page.evaluate("""
                ({
                    scrollHeight: document.body.scrollHeight,
                    clientHeight: document.documentElement.clientHeight,
                    scrollWidth:  document.body.scrollWidth,
                    clientWidth:  document.documentElement.clientWidth,
                    scrollTop:    window.scrollY,
                })
            """)
            return dims or {
                "scrollHeight": 3000,
                "clientHeight": self._viewport_h,
                "scrollTop":    0,
            }
        except Exception:
            return {
                "scrollHeight": 3000,
                "clientHeight": self._viewport_h,
                "scrollTop":    0,
            }

    # ── Core Scroll Methods ────────────────────────────────

    async def scroll_to_depth(
        self,
        target_depth:   float,
        read_as_scroll: bool    = True,
    ) -> int:
        """
        Scroll to a target depth (0.0-1.0).
        Returns final scroll position in pixels.
        """
        try:
            dims        = await self._get_page_dimensions()
            page_height = dims.get("scrollHeight", 3000)
            view_height = dims.get("clientHeight", self._viewport_h)
            max_scroll  = max(0, page_height - view_height)
            self._max_scroll    = max_scroll
            target_px           = int(max_scroll * target_depth)
            await self._scroll_to_position(
                target_px, read_as_scroll
            )
            return target_px
        except Exception as exc:
            logger.debug(f"[ScrollSimulator] Depth error: {exc}")
            return 0

    async def _scroll_to_position(
        self,
        target_px:      int,
        read_as_scroll: bool = True,
    ) -> None:
        """Scroll to a specific pixel position using wheel events."""
        speed_cfg   = self.SCROLL_SPEEDS.get(
            self._speed, self.SCROLL_SPEEDS["normal"]
        )
        current_pos = int(self._scroll_y)
        direction   = 1 if target_px > current_pos else -1
        remaining   = abs(target_px - current_pos)

        # Viewport center for wheel events
        center_x    = 960.0
        center_y    = self._viewport_h / 2

        while remaining > 10:
            # Variable delta per tick
            delta_min, delta_max = speed_cfg["delta"]
            tick_delta = random.randint(
                delta_min,
                min(delta_max, remaining),
            )

            # Momentum: occasionally scroll extra
            if random.random() < 0.15 and remaining > tick_delta * 3:
                tick_delta = int(tick_delta * random.uniform(1.5, 2.5))

            # Dispatch wheel event
            await self._dispatch_wheel(
                delta_y = direction * tick_delta,
                x       = center_x,
                y       = center_y,
            )

            current_pos += direction * tick_delta
            current_pos  = max(
                0, min(self._max_scroll, current_pos)
            )
            remaining    = abs(target_px - current_pos)

            # Inter-tick delay
            delay_min, delay_max = speed_cfg["delay"]
            await asyncio.sleep(random.uniform(delay_min, delay_max))

            # Reading pause during scroll
            if (
                read_as_scroll and
                random.random() < self._reading_pause_rate
            ):
                await asyncio.sleep(random.uniform(0.5, 2.5))

            # Back-scroll (re-reading behavior)
            if (
                direction == 1 and
                random.random() < self._back_scroll_rate and
                remaining > 200
            ):
                back    = random.randint(50, 200)
                await self._dispatch_wheel(
                    delta_y = -back,
                    x       = center_x,
                    y       = center_y,
                )
                current_pos = max(0, current_pos - back)
                await asyncio.sleep(random.uniform(0.3, 1.0))

    async def scroll_by(
        self,
        pixels:     int,
        smooth:     bool    = True,
    ) -> None:
        """Scroll by a relative pixel amount."""
        if not smooth or abs(pixels) < 50:
            await self._dispatch_wheel(
                delta_y = float(pixels),
            )
            await asyncio.sleep(random.uniform(0.05, 0.15))
            return

        # Smooth scroll with acceleration curve
        direction   = 1 if pixels > 0 else -1
        total       = abs(pixels)
        scrolled    = 0
        steps       = max(3, total // 80)

        for i in range(steps):
            # Bell curve speed profile
            t           = i / steps
            speed_mult  = math.sin(math.pi * t)
            step_px     = max(
                20,
                int((total / steps) * speed_mult * random.uniform(0.8, 1.2)),
            )
            step_px     = min(step_px, total - scrolled)

            if step_px <= 0:
                break

            await self._dispatch_wheel(
                delta_y=direction * step_px
            )
            scrolled += step_px

            await asyncio.sleep(random.uniform(0.02, 0.06))

    async def scroll_to_element(
        self,
        element:    Any,
        offset:     int     = -100,
        smooth:     bool    = True,
    ) -> bool:
        """Scroll element into view."""
        try:
            # Get element position
            bounds = await element.get_position()
            if not bounds:
                return False

            # Get current scroll position
            dims        = await self._get_page_dimensions()
            current_top = dims.get("scrollTop", 0)
            view_h      = dims.get("clientHeight", self._viewport_h)

            # Target scroll position
            el_top      = bounds.y + current_top
            target      = max(0, el_top - view_h // 2 + offset)

            # Scroll to element
            scroll_dist = target - current_top
            if abs(scroll_dist) > 10:
                await self.scroll_by(int(scroll_dist), smooth=smooth)

            await asyncio.sleep(random.uniform(0.3, 0.8))
            return True

        except Exception as exc:
            logger.debug(
                f"[ScrollSimulator] Scroll to element error: {exc}"
            )
            return False

    async def scroll_to_top(self) -> None:
        """Scroll back to top of page."""
        if self._scroll_y > 0:
            await self.scroll_by(
                -int(self._scroll_y), smooth=True
            )

    async def scroll_to_bottom(self) -> None:
        """Scroll to bottom of page."""
        dims = await self._get_page_dimensions()
        max_s = max(0,
            dims.get("scrollHeight", 3000) -
            dims.get("clientHeight", self._viewport_h)
        )
        remaining = max_s - int(self._scroll_y)
        if remaining > 0:
            await self.scroll_by(remaining, smooth=True)

    async def natural_page_read_scroll(
        self,
        target_depth:   float   = 0.7,
        read_speed:     float   = 1.0,
    ) -> None:
        """
        Simulate natural reading scroll pattern.
        Scrolls with pauses that mimic reading speed.
        """
        dims        = await self._get_page_dimensions()
        page_h      = dims.get("scrollHeight", 3000)
        view_h      = dims.get("clientHeight", self._viewport_h)
        max_scroll  = max(0, page_h - view_h)
        target_px   = int(max_scroll * target_depth)

        current     = 0
        # Average viewport scroll per "read"
        viewport_chunk = view_h * 0.6

        while current < target_px:
            # Scroll one viewport-ish amount
            chunk = random.uniform(
                viewport_chunk * 0.5,
                viewport_chunk * 1.2,
            )
            chunk = min(chunk, target_px - current)

            await self.scroll_by(int(chunk), smooth=True)
            current += chunk

            # Reading pause (proportional to text density)
            read_time = random.uniform(1.5, 5.0) / read_speed
            await asyncio.sleep(read_time)

            # Occasionally re-read (scroll up slightly)
            if random.random() < 0.15:
                back = random.uniform(50, 200)
                await self.scroll_by(-int(back), smooth=True)
                await asyncio.sleep(random.uniform(0.5, 1.5))

    def update_page(self, page: Any) -> None:
        """Update page reference after navigation."""
        self._page      = page
        self._scroll_y  = 0.0

    def reset(self) -> None:
        """Reset scroll position tracking."""
        self._current_scroll    = 0
        self._scroll_y          = 0.0

    @property
    def current_position(self) -> int:
        return int(self._scroll_y)

    def get_stats(self) -> Dict[str, Any]:
        return {
            "engine":           "nodriver_cdp",
            "current_scroll":   int(self._scroll_y),
            "max_scroll":       self._max_scroll,
            "speed_profile":    self._speed,
        }
