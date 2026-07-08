"""
Jubra Traffic Pro - Heatmap Simulator
Simulates Hotjar, Matomo, and Microsoft Clarity
heatmap and session recording data.
"""

import asyncio
import time
import random
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
from collections import deque

from core.config_manager import ConfigManager
from core.event_bus import EventBus, get_event_bus

logger = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Heatmap Data Points
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@dataclass
class HeatmapPoint:
    """A single heatmap data point."""
    x:          int
    y:          int
    event_type: str     = "move"    # move, click, scroll
    timestamp:  float   = field(default_factory=time.time)
    scroll_y:   int     = 0
    element:    str     = ""


@dataclass
class SessionRecording:
    """Session recording data for replay tools."""
    session_id:     str
    page_url:       str
    duration_ms:    int
    scroll_depth:   int
    click_count:    int
    move_count:     int
    rage_clicks:    int     = 0
    u_turns:        int     = 0
    dead_clicks:    int     = 0
    device_type:    str     = "desktop"
    browser:        str     = "Chrome"
    created_at:     float   = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id":   self.session_id[:16],
            "page_url":     self.page_url,
            "duration_ms":  self.duration_ms,
            "scroll_depth": self.scroll_depth,
            "click_count":  self.click_count,
            "move_count":   self.move_count,
            "device_type":  self.device_type,
        }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Heatmap Simulator
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class HeatmapSimulator:
    """
    Heatmap & Session Recording Simulator.

    Simulates activity for:
    ─────────────────────────────────────────────────────
    • Hotjar (JavaScript injection + API)
    • Microsoft Clarity (JavaScript injection)
    • Matomo (server-side API calls)
    • Generates realistic heatmap data
    • Click heatmaps with F-pattern bias
    • Scroll heatmaps with reading pattern
    • Move heatmaps with natural paths
    • Session replay data
    """

    # F-pattern reading areas (% of viewport)
    F_PATTERN_ZONES = [
        (0.0, 0.0, 1.0, 0.15),   # Top bar (most attention)
        (0.0, 0.15, 0.6, 0.35),  # Upper left content
        (0.0, 0.35, 0.4, 0.60),  # Left column
        (0.0, 0.60, 0.3, 0.85),  # Lower left
    ]

    def __init__(
        self,
        config:         ConfigManager,
        event_bus:      Optional[EventBus]  = None,
        provider:       str                 = "hotjar",
        hotjar_id:      str                 = "",
        inject_scripts: bool                = True,
    ):
        self._config        = config
        self._event_bus     = event_bus or get_event_bus()
        self._provider      = provider
        self._hotjar_id     = hotjar_id
        self._inject_scripts = inject_scripts

        # Session data
        self._sessions:     Dict[str, List[HeatmapPoint]] = {}
        self._recordings:   List[SessionRecording]        = []

        # Metrics
        self._total_points:     int = 0
        self._total_sessions:   int = 0

        logger.info(
            f"[HeatmapSimulator] Initialized: provider={provider}"
        )

    async def record_page_view(
        self,
        session_id:     str,
        page_url:       str,
        scroll_depth:   int     = 50,
        time_on_page_s: float   = 30.0,
        viewport_w:     int     = 1920,
        viewport_h:     int     = 1080,
        device_type:    str     = "desktop",
    ) -> SessionRecording:
        """
        Record a complete page view with synthetic heatmap data.
        Generates realistic click, scroll, and move patterns.
        """
        if session_id not in self._sessions:
            self._sessions[session_id] = []

        points: List[HeatmapPoint] = []

        # Generate move trail
        move_count  = int(time_on_page_s * random.uniform(3, 8))
        move_points = self._generate_move_trail(
            move_count, viewport_w, viewport_h
        )
        points.extend(move_points)

        # Generate clicks
        click_count = random.randint(0, max(1, int(time_on_page_s / 15)))
        click_points = self._generate_clicks(
            click_count, viewport_w, viewport_h
        )
        points.extend(click_points)

        # Generate scroll events
        scroll_points = self._generate_scroll_events(
            scroll_depth, viewport_h
        )
        points.extend(scroll_points)

        self._sessions[session_id].extend(points)
        self._total_points += len(points)

        # Create session recording
        recording = SessionRecording(
            session_id      = session_id,
            page_url        = page_url,
            duration_ms     = int(time_on_page_s * 1000),
            scroll_depth    = scroll_depth,
            click_count     = click_count,
            move_count      = move_count,
            rage_clicks     = 0,
            dead_clicks     = random.randint(0, 2),
            device_type     = device_type,
        )
        self._recordings.append(recording)
        self._total_sessions += 1

        logger.debug(
            f"[HeatmapSimulator] Recorded: {page_url[:40]} | "
            f"points={len(points)}, clicks={click_count}"
        )
        return recording

    def _generate_move_trail(
        self,
        count:      int,
        vw:         int,
        vh:         int,
    ) -> List[HeatmapPoint]:
        """Generate natural mouse movement trail with F-pattern bias."""
        points  = []
        # Start position
        cx = random.randint(vw // 4, 3 * vw // 4)
        cy = random.randint(vh // 8, vh // 3)

        for i in range(count):
            # Bias towards F-pattern zones
            if random.random() < 0.6:
                zone = random.choice(self.F_PATTERN_ZONES)
                cx   = int(vw * random.uniform(zone[0], zone[2]))
                cy   = int(vh * random.uniform(zone[1], zone[3]))
            else:
                # Small random movement from current position
                cx += random.randint(-50, 50)
                cy += random.randint(-30, 30)

            cx = max(0, min(vw, cx))
            cy = max(0, min(vh, cy))

            points.append(HeatmapPoint(
                x           = cx,
                y           = cy,
                event_type  = "move",
                timestamp   = time.time() + i * 0.2,
            ))

        return points

    def _generate_clicks(
        self,
        count:  int,
        vw:     int,
        vh:     int,
    ) -> List[HeatmapPoint]:
        """Generate click positions biased to interactive areas."""
        points = []

        # Click zones: nav, CTAs, content links
        click_zones = [
            (0.0, 0.0, 1.0, 0.08),    # Navigation bar
            (0.1, 0.15, 0.9, 0.35),   # Hero / top content
            (0.2, 0.35, 0.8, 0.65),   # Main content area
            (0.3, 0.65, 0.7, 0.85),   # Lower content
            (0.2, 0.85, 0.8, 1.0),    # Footer
        ]
        zone_weights = [0.25, 0.30, 0.25, 0.15, 0.05]

        for _ in range(count):
            # Select zone
            total = sum(zone_weights)
            r     = random.uniform(0, total)
            cumulative = 0.0
            zone  = click_zones[0]
            for z, w in zip(click_zones, zone_weights):
                cumulative += w
                if r <= cumulative:
                    zone = z
                    break

            x = int(vw * random.uniform(zone[0], zone[2]))
            y = int(vh * random.uniform(zone[1], zone[3]))

            points.append(HeatmapPoint(
                x           = x,
                y           = y,
                event_type  = "click",
                timestamp   = time.time() + random.uniform(0, 30),
            ))

        return points

    def _generate_scroll_events(
        self,
        max_depth_pct:  int,
        vh:             int,
    ) -> List[HeatmapPoint]:
        """Generate scroll depth events."""
        points      = []
        scroll_y    = 0
        page_height = vh * 3  # Assume 3x viewport height

        target_y = int(page_height * max_depth_pct / 100)

        while scroll_y < target_y:
            step     = random.randint(50, 150)
            scroll_y = min(scroll_y + step, target_y)

            points.append(HeatmapPoint(
                x           = 0,
                y           = 0,
                event_type  = "scroll",
                scroll_y    = scroll_y,
                timestamp   = time.time(),
            ))

        return points

    async def inject_hotjar(
        self,
        driver:     Any,
        hotjar_id:  str = "",
    ) -> bool:
        """Inject Hotjar tracking script into browser."""
        hj_id = hotjar_id or self._hotjar_id
        if not hj_id:
            return False

        script = f"""
        (function(h,o,t,j,a,r){{
            h.hj=h.hj||function(){{(h.hj.q=h.hj.q||[]).push(arguments)}};
            h._hjSettings={{hjid:{hj_id},hjsv:6}};
            a=o.getElementsByTagName('head')[0];
            r=o.createElement('script');r.async=1;
            r.src=t+h._hjSettings.hjid+j+h._hjSettings.hjsv;
            a.appendChild(r);
        }})(window,document,'https://static.hotjar.com/c/hotjar-','.js?sv=');
        """
        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None,
                lambda: driver.execute_script(script),
            )
            logger.debug(
                f"[HeatmapSimulator] Hotjar injected: {hj_id}"
            )
            return True
        except Exception as exc:
            logger.debug(f"[HeatmapSimulator] Inject error: {exc}")
            return False

    async def inject_clarity(
        self,
        driver:         Any,
        clarity_id:     str,
    ) -> bool:
        """Inject Microsoft Clarity tracking script."""
        script = f"""
        (function(c,l,a,r,i,t,y){{
            c[a]=c[a]||function(){{(c[a].q=c[a].q||[]).push(arguments)}};
            t=l.createElement(r);t.async=1;t.src="https://www.clarity.ms/tag/"+i;
            y=l.getElementsByTagName(r)[0];y.parentNode.insertBefore(t,y);
        }})(window, document, "clarity", "script", "{clarity_id}");
        """
        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None,
                lambda: driver.execute_script(script),
            )
            return True
        except Exception as exc:
            logger.debug(f"[HeatmapSimulator] Clarity inject error: {exc}")
            return False

    def get_session_recording(
        self,
        session_id: str,
    ) -> Optional[SessionRecording]:
        """Get the recording for a session."""
        for rec in reversed(self._recordings):
            if rec.session_id == session_id:
                return rec
        return None

    def export_heatmap_data(
        self,
        session_id: str,
        event_type: str = "click",
    ) -> List[Dict[str, int]]:
        """Export heatmap points for visualization."""
        points = self._sessions.get(session_id, [])
        return [
            {"x": p.x, "y": p.y}
            for p in points
            if p.event_type == event_type
        ]

    def get_metrics(self) -> Dict[str, Any]:
        return {
            "total_sessions":   self._total_sessions,
            "total_points":     self._total_points,
            "provider":         self._provider,
            "recordings":       len(self._recordings),
            "active_sessions":  len(self._sessions),
        }