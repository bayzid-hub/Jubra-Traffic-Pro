"""
Jubra Traffic Pro - Mouse Dynamics (nodriver Edition)
Bézier curve mouse simulation using nodriver's
native CDP Input domain - no Selenium ActionChains needed.
"""

import asyncio
import math
import random
import time
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
from collections import deque
from enum import Enum, auto

try:
    import nodriver as uc
    HAS_NODRIVER = True
except ImportError:
    HAS_NODRIVER = False

logger = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Mouse Point
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@dataclass
class MousePoint:
    """A single point in a mouse trajectory."""
    x:          float
    y:          float
    timestamp:  float   = field(default_factory=time.monotonic)
    velocity:   float   = 0.0

    def distance_to(self, other: "MousePoint") -> float:
        return math.sqrt(
            (self.x - other.x)**2 +
            (self.y - other.y)**2
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Bézier Engine
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class BezierEngine:
    """Generates human-like mouse paths using Bézier curves."""

    FITTS_A = 0.150
    FITTS_B = 0.120

    def generate_path(
        self,
        start:          Tuple[float, float],
        end:            Tuple[float, float],
        speed_factor:   float   = 1.0,
        curviness:      float   = 0.3,
    ) -> List[MousePoint]:
        """Generate natural mouse path from start to end."""
        sx, sy  = start
        ex, ey  = end
        distance = math.sqrt((ex-sx)**2 + (ey-sy)**2)

        if distance < 2:
            return [MousePoint(x=ex, y=ey)]

        # Fitts's Law duration
        target_w    = max(5.0, distance * 0.05)
        fitts_mt    = self.FITTS_A + self.FITTS_B * math.log2(
            1 + distance / target_w
        )
        duration_s  = fitts_mt / speed_factor

        # Number of segments
        n_segments  = max(1, int(distance / 150))
        all_points: List[MousePoint] = []
        current     = (sx, sy)

        do_overshoot = random.random() < 0.25 and distance > 80

        for seg_idx in range(n_segments):
            is_last = (seg_idx == n_segments - 1)

            if is_last:
                target = (ex, ey)
            else:
                t       = (seg_idx + 1) / n_segments
                mid_x   = sx + (ex - sx) * t
                mid_y   = sy + (ey - sy) * t
                dev     = distance * random.uniform(-0.08, 0.08)
                angle   = math.atan2(ey-sy, ex-sx) + math.pi/2
                target  = (
                    mid_x + dev * math.cos(angle),
                    mid_y + dev * math.sin(angle),
                )

            seg_pts = self._cubic_bezier(
                start       = current,
                end         = target,
                curviness   = curviness,
                steps       = max(8, int(
                    math.sqrt(
                        (target[0]-current[0])**2 +
                        (target[1]-current[1])**2
                    ) / 100 * 12
                )),
                duration_s  = duration_s / n_segments,
            )
            all_points.extend(seg_pts)
            current = target

        if do_overshoot and all_points:
            overshoot = self._overshoot(all_points, (ex, ey), distance)
            all_points.extend(overshoot)

        all_points = self._inject_tremor(all_points)
        all_points = self._compute_velocities(all_points)
        return all_points

    def _cubic_bezier(
        self,
        start:      Tuple[float, float],
        end:        Tuple[float, float],
        curviness:  float,
        steps:      int,
        duration_s: float,
    ) -> List[MousePoint]:
        sx, sy  = start
        ex, ey  = end
        dx, dy  = ex-sx, ey-sy
        dist    = math.sqrt(dx**2 + dy**2)
        if dist < 1:
            return [MousePoint(x=ex, y=ey)]

        perp    = math.atan2(dy, dx) + math.pi/2
        offset  = dist * curviness * random.uniform(0.3, 0.7)
        cp_dir  = random.choice([-1, 1])

        p0 = (sx, sy)
        p3 = (ex, ey)
        p1 = (
            sx + dx*0.25 + cp_dir*offset*math.cos(perp)*0.5,
            sy + dy*0.25 + cp_dir*offset*math.sin(perp)*0.5,
        )
        p2 = (
            sx + dx*0.75 - cp_dir*offset*math.cos(perp)*0.3,
            sy + dy*0.75 - cp_dir*offset*math.sin(perp)*0.3,
        )

        t_start = time.monotonic()
        points  = []
        for i in range(steps + 1):
            t   = i / steps
            # Ease-in-out
            t   = t*t*(3 - 2*t)
            mt  = 1 - t
            x   = mt**3*p0[0] + 3*mt**2*t*p1[0] + 3*mt*t**2*p2[0] + t**3*p3[0]
            y   = mt**3*p0[1] + 3*mt**2*t*p1[1] + 3*mt*t**2*p2[1] + t**3*p3[1]
            ts  = t_start + (i / max(steps,1)) * duration_s
            points.append(MousePoint(x=x, y=y, timestamp=ts))
        return points

    def _overshoot(
        self,
        points:     List[MousePoint],
        target:     Tuple[float, float],
        distance:   float,
    ) -> List[MousePoint]:
        tx, ty  = target
        o_dist  = distance * random.uniform(0.02, 0.08)
        angle   = math.atan2(
            ty - points[-2].y if len(points) > 1 else 0,
            tx - points[-2].x if len(points) > 1 else 1,
        )
        ox = tx + o_dist * math.cos(angle)
        oy = ty + o_dist * math.sin(angle)

        steps   = random.randint(3, 8)
        t_start = time.monotonic()
        result  = []
        for i in range(steps + 1):
            t       = i / steps
            t_ease  = 1 - (1-t)**2
            cx      = ox + (tx-ox) * t_ease
            cy      = oy + (ty-oy) * t_ease
            result.append(MousePoint(
                x=cx, y=cy,
                timestamp=t_start + i*0.012,
            ))
        return result

    def _inject_tremor(
        self,
        points:     List[MousePoint],
        intensity:  float   = 0.4,
        frequency:  float   = 8.0,
    ) -> List[MousePoint]:
        result = []
        for i, pt in enumerate(points):
            t_norm  = i / max(len(points)-1, 1)
            env     = math.exp(-((t_norm-0.5)**2) / 0.15)
            tx = intensity*env*math.sin(
                2*math.pi*frequency*pt.timestamp +
                random.uniform(0, 0.3)
            ) * random.uniform(0.3, 1.0)
            ty = intensity*env*math.cos(
                2*math.pi*frequency*pt.timestamp +
                random.uniform(0, 0.3)
            ) * random.uniform(0.3, 1.0)
            result.append(MousePoint(
                x=pt.x+tx, y=pt.y+ty,
                timestamp=pt.timestamp,
            ))
        return result

    def _compute_velocities(
        self,
        points: List[MousePoint],
    ) -> List[MousePoint]:
        if len(points) < 2:
            return points
        for i in range(1, len(points)):
            dt = points[i].timestamp - points[i-1].timestamp
            if dt > 0:
                dx = points[i].x - points[i-1].x
                dy = points[i].y - points[i-1].y
                points[i].velocity = math.sqrt(dx**2+dy**2) / dt
        return points


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Mouse Dynamics (nodriver)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class MouseDynamics:
    """
    nodriver Mouse Dynamics Engine.

    Uses CDP Input domain directly:
    ─────────────────────────────────────────────────────
    • Input.dispatchMouseEvent via page.send()
    • No Selenium ActionChains dependency
    • Native async mouse movement
    • Real CDP mouse events (not JS simulation)
    • Bézier curve paths with tremor
    • Fitts's Law timing
    """

    SPEED_PROFILES = {
        "slow":     (0.4, 0.7),
        "normal":   (0.8, 1.2),
        "fast":     (1.3, 1.8),
        "erratic":  (0.3, 2.5),
    }

    def __init__(
        self,
        page:               Any,    # nodriver Tab
        viewport_width:     int     = 1920,
        viewport_height:    int     = 1080,
        speed_profile:      str     = "normal",
        base_curviness:     float   = 0.3,
        tremor_intensity:   float   = 0.4,
    ):
        self._page          = page
        self._width         = viewport_width
        self._height        = viewport_height
        self._speed_profile = speed_profile
        self._base_curviness = base_curviness
        self._tremor        = tremor_intensity

        self._current_x:    float   = viewport_width  / 2
        self._current_y:    float   = viewport_height / 2
        self._bezier        = BezierEngine()
        self._history:      deque   = deque(maxlen=2000)

        # Stats
        self._total_moves:  int     = 0
        self._total_clicks: int     = 0
        self._total_dist:   float   = 0.0

    # ── CDP Mouse Event Dispatcher ─────────────────────────

    async def _dispatch_mouse_event(
        self,
        event_type: str,
        x:          float,
        y:          float,
        button:     str     = "none",
        click_count: int    = 0,
        delta_x:    float   = 0.0,
        delta_y:    float   = 0.0,
    ) -> None:
        """
        Dispatch mouse event via CDP Input domain.
        This is more realistic than JS dispatchEvent.
        """
        try:
            await self._page.send(
                uc.cdp.input_.dispatch_mouse_event(
                    type_           = event_type,
                    x               = x,
                    y               = y,
                    button          = uc.cdp.input_.MouseButton(button)
                    if button != "none"
                    else uc.cdp.input_.MouseButton.NONE,
                    click_count     = click_count,
                    delta_x         = delta_x,
                    delta_y         = delta_y,
                    modifiers       = 0,
                )
            )
        except Exception as exc:
            logger.debug(f"[MouseDynamics] CDP event error: {exc}")

    # ── Movement ───────────────────────────────────────────

    async def move_to(
        self,
        target_x:   float,
        target_y:   float,
        speed:      Optional[str]   = None,
        curviness:  Optional[float] = None,
    ) -> List[MousePoint]:
        """Move mouse to target using Bézier curve path."""
        target_x = max(0, min(target_x, self._width  - 1))
        target_y = max(0, min(target_y, self._height - 1))

        speed_min, speed_max = self.SPEED_PROFILES.get(
            speed or self._speed_profile,
            self.SPEED_PROFILES["normal"],
        )
        speed_factor    = random.uniform(speed_min, speed_max)
        curve           = curviness if curviness is not None else (
            self._base_curviness * random.uniform(0.7, 1.3)
        )

        path = self._bezier.generate_path(
            start           = (self._current_x, self._current_y),
            end             = (target_x, target_y),
            speed_factor    = speed_factor,
            curviness       = curve,
        )

        if not path:
            return []

        await self._execute_path(path)

        self._current_x = target_x
        self._current_y = target_y
        self._total_moves += 1
        self._total_dist  += math.sqrt(
            (target_x - path[0].x)**2 +
            (target_y - path[0].y)**2
        )
        return path

    async def _execute_path(self, path: List[MousePoint]) -> None:
        """Execute mouse path via CDP mouse events."""
        if not path:
            return

        prev_ts = path[0].timestamp

        for pt in path:
            # Dispatch mousemove via CDP
            await self._dispatch_mouse_event(
                event_type  = "mouseMoved",
                x           = round(pt.x, 1),
                y           = round(pt.y, 1),
            )

            # Real timing delay
            dt = pt.timestamp - prev_ts
            if dt > 0.001:
                await asyncio.sleep(dt * 0.8)
            prev_ts = pt.timestamp

            self._history.append(pt)

        # Occasional micro-pause
        if random.random() < 0.15:
            await asyncio.sleep(random.uniform(0.05, 0.18))

    # ── Click Operations ───────────────────────────────────

    async def click(
        self,
        x:          float,
        y:          float,
        button:     str                         = "left",
        move_first: bool                        = True,
        pre_delay:  Optional[Tuple[float,float]] = None,
        post_delay: Optional[Tuple[float,float]] = None,
    ) -> bool:
        """Perform a natural mouse click via CDP."""
        try:
            if move_first:
                await self.move_to(x, y)

            # Micro jitter
            cx = x + random.uniform(-3, 3)
            cy = y + random.uniform(-3, 3)

            # Pre-click delay
            min_d, max_d = pre_delay or (0.05, 0.15)
            await asyncio.sleep(random.uniform(min_d, max_d))

            # Mouse down
            await self._dispatch_mouse_event(
                event_type  = "mousePressed",
                x           = cx,
                y           = cy,
                button      = button,
                click_count = 1,
            )

            # Hold duration (human: 80-150ms)
            await asyncio.sleep(random.uniform(0.08, 0.15))

            # Mouse up
            await self._dispatch_mouse_event(
                event_type  = "mouseReleased",
                x           = cx,
                y           = cy,
                button      = button,
                click_count = 1,
            )

            # Post-click delay
            min_d, max_d = post_delay or (0.08, 0.25)
            await asyncio.sleep(random.uniform(min_d, max_d))

            self._total_clicks += 1
            self._history.append(MousePoint(x=cx, y=cy))

            logger.debug(
                f"[MouseDynamics] Click at "
                f"({round(cx)}, {round(cy)})"
            )
            return True

        except Exception as exc:
            logger.debug(f"[MouseDynamics] Click error: {exc}")
            return False

    async def double_click(
        self,
        x:  float,
        y:  float,
    ) -> bool:
        """Double-click with realistic timing."""
        try:
            if not await self.click(x, y):
                return False

            # Inter-click interval (80-350ms)
            await asyncio.sleep(random.uniform(0.08, 0.35))

            cx = x + random.uniform(-2, 2)
            cy = y + random.uniform(-2, 2)

            # Second click
            await self._dispatch_mouse_event(
                "mousePressed", cx, cy, "left", click_count=2
            )
            await asyncio.sleep(random.uniform(0.08, 0.15))
            await self._dispatch_mouse_event(
                "mouseReleased", cx, cy, "left", click_count=2
            )
            return True

        except Exception as exc:
            logger.debug(f"[MouseDynamics] DblClick error: {exc}")
            return False

    async def right_click(self, x: float, y: float) -> bool:
        """Right click via CDP."""
        return await self.click(x, y, button="right")

    async def scroll(
        self,
        x:          float,
        y:          float,
        delta_y:    float   = 100.0,
        delta_x:    float   = 0.0,
    ) -> bool:
        """Scroll at position via CDP."""
        try:
            await self._dispatch_mouse_event(
                event_type  = "mouseWheel",
                x           = x,
                y           = y,
                delta_x     = delta_x,
                delta_y     = delta_y,
            )
            return True
        except Exception as exc:
            logger.debug(f"[MouseDynamics] Scroll error: {exc}")
            return False

    async def drag(
        self,
        start_x:    float,
        start_y:    float,
        end_x:      float,
        end_y:      float,
        speed:      str = "normal",
    ) -> bool:
        """Drag from start to end."""
        try:
            await self.move_to(start_x, start_y)
            await asyncio.sleep(random.uniform(0.05, 0.15))

            # Mouse down
            await self._dispatch_mouse_event(
                "mousePressed", start_x, start_y, "left"
            )

            # Move along path
            path = self._bezier.generate_path(
                start           = (start_x, start_y),
                end             = (end_x, end_y),
                speed_factor    = 0.6,
                curviness       = 0.1,
            )
            await self._execute_path(path)

            # Mouse up
            await self._dispatch_mouse_event(
                "mouseReleased", end_x, end_y, "left"
            )

            self._current_x = end_x
            self._current_y = end_y
            return True

        except Exception as exc:
            logger.debug(f"[MouseDynamics] Drag error: {exc}")
            return False

    async def move_to_element(
        self,
        element:    Any,
        offset_x:   float = 0,
        offset_y:   float = 0,
    ) -> bool:
        """Move mouse to a nodriver element."""
        try:
            # Get element bounding box via nodriver
            bounds = await element.get_position()

            if not bounds:
                return False

            # Center of element
            cx = bounds.x + bounds.width  / 2 + offset_x
            cy = bounds.y + bounds.height / 2 + offset_y

            # Add slight randomness
            cx += random.uniform(
                -bounds.width  * 0.1,
                bounds.width   * 0.1,
            )
            cy += random.uniform(
                -bounds.height * 0.1,
                bounds.height  * 0.1,
            )

            await self.move_to(cx, cy)
            return True

        except Exception as exc:
            logger.debug(
                f"[MouseDynamics] Move to element error: {exc}"
            )
            return False

    async def idle_movement(
        self,
        duration_s: float = 2.0,
    ) -> None:
        """Simulate idle micro-movements."""
        end_time = time.monotonic() + duration_s
        while time.monotonic() < end_time:
            dx  = random.gauss(0, 8)
            dy  = random.gauss(0, 6)
            nx  = max(5, min(self._width  - 5, self._current_x + dx))
            ny  = max(5, min(self._height - 5, self._current_y + dy))

            await self.move_to(nx, ny, speed="slow")
            await asyncio.sleep(random.uniform(0.3, 1.2))

    def update_page(self, page: Any) -> None:
        """Update the page reference (after navigation)."""
        self._page = page

    @property
    def current_position(self) -> Tuple[float, float]:
        return (self._current_x, self._current_y)

    def set_position(self, x: float, y: float) -> None:
        self._current_x = x
        self._current_y = y

    def get_stats(self) -> Dict[str, Any]:
        return {
            "engine":           "nodriver_cdp",
            "total_moves":      self._total_moves,
            "total_clicks":     self._total_clicks,
            "total_distance":   round(self._total_dist, 1),
            "current_position": {
                "x": round(self._current_x),
                "y": round(self._current_y),
            },
            "speed_profile":    self._speed_profile,
        }