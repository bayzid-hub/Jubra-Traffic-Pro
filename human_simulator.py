"""
Jubra Traffic Pro - Human Simulator (nodriver Edition)
Master coordinator for all human-like behaviors using nodriver.
No Selenium dependency.
"""

import asyncio
import time
import random
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

from behavior.mouse_dynamics    import MouseDynamics
from behavior.keyboard_dynamics import KeyboardDynamics
from behavior.scroll_simulator  import ScrollSimulator

logger = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Behavior Action
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class BehaviorAction(Enum):
    NAVIGATE        = "navigate"
    SCROLL_DOWN     = "scroll_down"
    SCROLL_UP       = "scroll_up"
    CLICK_LINK      = "click_link"
    CLICK_BUTTON    = "click_button"
    FILL_FORM       = "fill_form"
    SEARCH          = "search"
    READ_CONTENT    = "read_content"
    IDLE_PAUSE      = "idle_pause"
    HOVER_ELEMENT   = "hover_element"
    SELECT_TEXT     = "select_text"
    EXIT_INTENT     = "exit_intent"


@dataclass
class BehaviorEvent:
    """Record of a behavior action."""
    action:         BehaviorAction
    timestamp:      float
    duration_ms:    float       = 0.0
    target:         str         = ""
    success:        bool        = True
    metadata:       Dict        = field(default_factory=dict)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Reading Model
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class ReadingModel:
    """Models human reading behavior."""

    AVG_READING_WPM     = 238.0

    CONTENT_MULTIPLIERS = {
        "article":  1.00,
        "product":  0.60,
        "homepage": 0.40,
        "blog":     1.10,
        "news":     0.85,
        "landing":  0.50,
        "form":     0.30,
        "search":   0.25,
        "video":    0.10,
    }

    SCROLL_DEPTHS = {
        "article":  0.85,
        "product":  0.70,
        "homepage": 0.55,
        "blog":     0.80,
        "news":     0.65,
        "landing":  0.60,
        "search":   0.50,
    }

    def estimate_read_time(
        self,
        word_count:     int,
        content_type:   str     = "article",
        read_speed:     float   = 1.0,
    ) -> float:
        """Estimate seconds to read content."""
        mult        = self.CONTENT_MULTIPLIERS.get(content_type, 1.0)
        base_wpm    = self.AVG_READING_WPM * mult * read_speed
        base_time   = (word_count / base_wpm) * 60
        variation   = random.uniform(0.75, 1.35)
        read_time   = base_time * variation
        min_time    = max(3.0, word_count * 0.01)
        return max(min_time, read_time)

    def get_scroll_depth(
        self,
        content_type:   str,
        engagement:     float   = 0.7,
    ) -> float:
        """Estimate scroll depth for content type."""
        base    = self.SCROLL_DEPTHS.get(content_type, 0.65)
        depth   = base * engagement * random.uniform(0.8, 1.2)
        return min(1.0, max(0.1, depth))


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Human Simulator (nodriver)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class HumanSimulator:
    """
    nodriver Human Behavior Simulator.

    Key changes from Selenium version:
    ─────────────────────────────────────────────────────
    • All element finding uses nodriver's page.select()
    • Mouse events via CDP Input domain
    • Keyboard events via CDP Input domain
    • Scroll via CDP mouseWheel events
    • Page updates propagated after navigation
    • No WebDriverWait / By.CSS_SELECTOR
    """

    def __init__(
        self,
        page:               Any,    # nodriver Tab
        session_identity:   Any     = None,
        viewport_width:     int     = 1920,
        viewport_height:    int     = 1080,
        read_speed:         float   = 1.0,
        scroll_speed:       str     = "normal",
        mouse_speed:        str     = "normal",
        typing_wpm_min:     int     = 40,
        typing_wpm_max:     int     = 80,
        typo_rate:          float   = 0.04,
        engagement_level:   float   = 0.7,
        idle_rate:          float   = 0.08,
    ):
        self._page          = page
        self._identity      = session_identity
        self._viewport_w    = viewport_width
        self._viewport_h    = viewport_height
        self._read_speed    = read_speed
        self._idle_rate     = idle_rate
        self._engagement    = engagement_level

        # Sub-simulators - all use nodriver page
        self._mouse = MouseDynamics(
            page            = page,
            viewport_width  = viewport_width,
            viewport_height = viewport_height,
            speed_profile   = mouse_speed,
        )
        self._keyboard = KeyboardDynamics(
            page        = page,
            wpm_min     = typing_wpm_min,
            wpm_max     = typing_wpm_max,
            typo_rate   = typo_rate,
        )
        self._scroll = ScrollSimulator(
            page            = page,
            speed_profile   = scroll_speed,
            viewport_height = viewport_height,
        )
        self._reading_model = ReadingModel()

        # Behavior history
        self._events:       deque   = deque(maxlen=500)
        self._start_time:   float   = time.monotonic()
        self._pages_read:   int     = 0

        logger.debug(
            f"[HumanSimulator] Initialized (nodriver): "
            f"viewport={viewport_width}x{viewport_height}"
        )

    # ── Page Update ────────────────────────────────────────

    def update_page(self, page: Any) -> None:
        """
        Update page reference after navigation.
        MUST be called after every page.get() / navigation.
        """
        self._page = page
        self._mouse.update_page(page)
        self._keyboard.update_page(page)
        self._scroll.update_page(page)
        self._scroll.reset()
        self._mouse.set_position(
            self._viewport_w / 2,
            self._viewport_h / 2,
        )

    # ── Page Reading ───────────────────────────────────────

    async def simulate_page_read(
        self,
        content_type:   str     = "article",
        word_count:     int     = 500,
        scroll:         bool    = True,
        interact:       bool    = True,
    ) -> Dict[str, Any]:
        """
        Simulate a complete page reading session.
        Uses nodriver for all interactions.
        """
        start   = time.monotonic()
        results = {
            "content_type": content_type,
            "word_count":   word_count,
            "scroll_depth": 0.0,
            "time_on_page": 0.0,
            "interactions": 0,
            "actions":      [],
        }

        # Initial scan pause
        await asyncio.sleep(random.uniform(0.8, 2.5))

        # Random mouse scan
        if random.random() < 0.7:
            await self._random_mouse_scan(count=2)

        # Determine scroll depth
        target_depth = self._reading_model.get_scroll_depth(
            content_type, self._engagement
        )
        results["scroll_depth"] = target_depth

        # Scroll with reading
        if scroll and target_depth > 0.1:
            await self._event(BehaviorAction.SCROLL_DOWN)
            await self._scroll.scroll_to_depth(
                target_depth,
                read_as_scroll=True,
            )
            results["actions"].append("scrolled")

        # Reading time
        read_time = self._reading_model.estimate_read_time(
            word_count      = word_count,
            content_type    = content_type,
            read_speed      = self._read_speed,
        )
        await self._simulate_reading_time(read_time, results)

        # Random interactions
        if interact and random.random() < 0.35:
            await self._random_interaction(content_type)
            results["interactions"] += 1

        # Idle check
        if random.random() < self._idle_rate:
            idle_t = random.uniform(5, 25)
            await asyncio.sleep(idle_t)
            await self._event(BehaviorAction.IDLE_PAUSE)
            results["actions"].append(f"idle_{idle_t:.0f}s")

        # Scroll back up sometimes
        if (
            random.random() < 0.20 and
            self._scroll.current_position > 200
        ):
            up = random.randint(100, 500)
            await self._scroll.scroll_by(-up)
            results["actions"].append("scroll_back")

        results["time_on_page"] = time.monotonic() - start
        self._pages_read += 1
        await self._event(
            BehaviorAction.READ_CONTENT,
            metadata=results,
        )
        return results

    # ── Search Simulation ──────────────────────────────────

    async def simulate_search(
        self,
        keyword:            str,
        search_box_selector: str = "input[name='q'], input[type='search'], textarea[name='q']",
    ) -> bool:
        """Simulate typing a search query."""
        try:
            # Find search box using nodriver
            search_box = None
            for selector in search_box_selector.split(","):
                try:
                    search_box = await self._page.select(
                        selector.strip(),
                        timeout=5,
                    )
                    if search_box:
                        break
                except Exception:
                    continue

            if not search_box:
                logger.debug("[HumanSimulator] Search box not found")
                return False

            # Move to search box
            await self._mouse.move_to_element(search_box)
            await asyncio.sleep(random.uniform(0.2, 0.5))

            # Click search box
            bounds = await search_box.get_position()
            if bounds:
                await self._mouse.click(
                    bounds.x + bounds.width  / 2,
                    bounds.y + bounds.height / 2,
                    move_first=False,
                )

            # Type query
            await self._keyboard.type_search_query(
                keyword, search_box
            )

            # Press Enter
            await self._keyboard.press_key("ENTER")

            await self._event(
                BehaviorAction.SEARCH,
                target=keyword,
            )
            return True

        except Exception as exc:
            logger.debug(f"[HumanSimulator] Search error: {exc}")
            return False

    # ── Form Fill ──────────────────────────────────────────

    async def simulate_form_fill(
        self,
        form_data:  Dict[str, str],
        selectors:  Dict[str, str],
    ) -> bool:
        """Fill a form with realistic field-by-field interaction."""
        success_count = 0

        for field_name, value in form_data.items():
            selector = selectors.get(
                field_name,
                f"input[name='{field_name}']",
            )
            try:
                element = await self._page.select(
                    selector, timeout=5
                )
                if not element:
                    continue

                # Move and click field
                await self._mouse.move_to_element(element)
                await asyncio.sleep(random.uniform(0.1, 0.4))

                bounds = await element.get_position()
                if bounds:
                    await self._mouse.click(
                        bounds.x + bounds.width  / 2,
                        bounds.y + bounds.height / 2,
                        move_first=False,
                    )

                # Type value
                is_password = "password" in field_name.lower()
                await self._keyboard.type_text(
                    text        = value,
                    element     = element,
                    clear       = True,
                    password    = is_password,
                )

                success_count += 1

                # Tab to next field sometimes
                if random.random() < 0.5:
                    await self._keyboard.press_key("TAB")

                # Inter-field pause
                await asyncio.sleep(random.uniform(0.3, 1.2))

            except Exception as exc:
                logger.debug(
                    f"[HumanSimulator] Form field error "
                    f"'{field_name}': {exc}"
                )

        await self._event(
            BehaviorAction.FILL_FORM,
            metadata={"fields_filled": success_count},
        )
        return success_count > 0

    # ── Link Click ─────────────────────────────────────────

    async def simulate_link_click(
        self,
        element: Any,
    ) -> bool:
        """Click a link with natural hover-then-click behavior."""
        try:
            # Move to link
            await self._mouse.move_to_element(element)

            # Hover pause (reading link text)
            await asyncio.sleep(random.uniform(0.15, 0.6))

            # Possible micro-movement
            if random.random() < 0.3:
                cx, cy = self._mouse.current_position
                await self._mouse.move_to(
                    cx + random.uniform(-5, 5),
                    cy + random.uniform(-3, 3),
                )

            # Click via nodriver element
            await element.click()

            await self._event(BehaviorAction.CLICK_LINK)
            return True

        except Exception as exc:
            logger.debug(
                f"[HumanSimulator] Link click error: {exc}"
            )
            return False

    # ── Exit Intent ────────────────────────────────────────

    async def simulate_exit_intent(self) -> None:
        """Simulate exit intent: mouse moves toward top."""
        target_x = random.uniform(100, self._viewport_w - 100)
        target_y = random.uniform(0, 30)
        await self._mouse.move_to(
            target_x, target_y, speed="fast"
        )
        await asyncio.sleep(random.uniform(0.2, 0.8))
        await self._event(BehaviorAction.EXIT_INTENT)

    # ── Internal Helpers ───────────────────────────────────

    async def _simulate_reading_time(
        self,
        duration:   float,
        results:    Dict,
    ) -> None:
        """Simulate reading time with occasional mouse movements."""
        elapsed     = 0.0
        interval    = 3.0

        while elapsed < duration:
            wait    = min(interval, duration - elapsed)
            await asyncio.sleep(wait)
            elapsed += wait

            # Occasional mouse micro-movement
            if random.random() < 0.25:
                cx, cy = self._mouse.current_position
                await self._mouse.move_to(
                    cx + random.gauss(0, 30),
                    cy + random.gauss(0, 20),
                    speed="slow",
                )

            # Tiny scroll occasionally
            if random.random() < 0.15:
                direction = random.choice([-1, 1])
                await self._scroll.scroll_by(
                    direction * random.randint(30, 80)
                )

    async def _random_mouse_scan(self, count: int = 3) -> None:
        """Random scanning mouse movements."""
        for _ in range(count):
            x = random.uniform(
                self._viewport_w * 0.1,
                self._viewport_w * 0.9,
            )
            y = random.uniform(
                self._viewport_h * 0.1,
                self._viewport_h * 0.7,
            )
            await self._mouse.move_to(x, y, speed="normal")
            await asyncio.sleep(random.uniform(0.1, 0.4))

    async def _random_interaction(
        self,
        content_type: str,
    ) -> None:
        """Perform a random interaction on the page."""
        action = random.choice([
            "hover_element",
            "select_text",
            "micro_scroll",
        ])

        if action == "hover_element":
            x = random.uniform(
                self._viewport_w * 0.2,
                self._viewport_w * 0.8,
            )
            y = random.uniform(
                self._viewport_h * 0.3,
                self._viewport_h * 0.8,
            )
            await self._mouse.move_to(x, y)
            await asyncio.sleep(random.uniform(0.3, 1.0))
            await self._event(BehaviorAction.HOVER_ELEMENT)

        elif action == "select_text":
            x1  = random.uniform(100, self._viewport_w - 200)
            y1  = random.uniform(200, self._viewport_h - 200)
            await self._mouse.drag(
                x1, y1,
                x1 + random.uniform(80, 250),
                y1,
            )
            await asyncio.sleep(random.uniform(0.3, 0.8))
            await self._event(BehaviorAction.SELECT_TEXT)

        elif action == "micro_scroll":
            direction = random.choice([-1, 1])
            await self._scroll.scroll_by(
                direction * random.randint(50, 150)
            )

    async def _event(
        self,
        action:     BehaviorAction,
        target:     str     = "",
        success:    bool    = True,
        metadata:   Dict    = None,
    ) -> None:
        """Record a behavior event."""
        self._events.append(BehaviorEvent(
            action      = action,
            timestamp   = time.monotonic(),
            target      = target,
            success     = success,
            metadata    = metadata or {},
        ))

    # ── Properties ─────────────────────────────────────────

    @property
    def mouse(self) -> MouseDynamics:
        return self._mouse

    @property
    def keyboard(self) -> KeyboardDynamics:
        return self._keyboard

    @property
    def scroll(self) -> ScrollSimulator:
        return self._scroll

    async def get_word_count(self) -> int:
        """Get word count of current page."""
        try:
            count = await self._page.evaluate(
                "document.body ? "
                "document.body.innerText.split(/\\s+/).length : 300"
            )
            return max(50, int(count))
        except Exception:
            return 300

    def get_session_stats(self) -> Dict[str, Any]:
        action_counts: Dict[str, int] = {}
        for evt in self._events:
            key = evt.action.value
            action_counts[key] = action_counts.get(key, 0) + 1

        return {
            "engine":           "nodriver",
            "pages_read":       self._pages_read,
            "total_events":     len(self._events),
            "session_duration": round(
                time.monotonic() - self._start_time, 2
            ),
            "action_counts":    action_counts,
            "mouse_stats":      self._mouse.get_stats(),
            "keyboard_stats":   self._keyboard.get_stats(),
            "scroll_position":  self._scroll.current_position,
        }