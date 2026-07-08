"""
Jubra Traffic Pro - Mobile Device Emulator
Complete mobile device profile management with
realistic touch, viewport, and sensor simulation.
"""

import asyncio
import json
import random
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Device Profile
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@dataclass
class MobileDevice:
    """Complete mobile device profile."""
    name:               str
    user_agent:         str
    screen_width:       int
    screen_height:      int
    viewport_width:     int
    viewport_height:    int
    pixel_ratio:        float
    touch_points:       int
    os_family:          str
    os_version:         str
    browser_version:    str
    is_tablet:          bool        = False
    has_gyroscope:      bool        = True
    has_accelerometer:  bool        = True
    has_gps:            bool        = True
    battery_level:      float       = 0.85
    orientation:        str         = "portrait-primary"

    def to_cdp_metrics(self) -> Dict[str, Any]:
        """Get CDP device metrics override parameters."""
        return {
            "width":            self.viewport_width,
            "height":           self.viewport_height,
            "deviceScaleFactor": self.pixel_ratio,
            "mobile":           True,
        }

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name":         self.name,
            "screen":       f"{self.screen_width}x{self.screen_height}",
            "viewport":     f"{self.viewport_width}x{self.viewport_height}",
            "pixel_ratio":  self.pixel_ratio,
            "user_agent":   self.user_agent[:60] + "...",
            "os":           f"{self.os_family} {self.os_version}",
            "is_tablet":    self.is_tablet,
        }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Device Database
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class MobileDeviceDB:
    """Database of real mobile device profiles."""

    DEVICES: Dict[str, MobileDevice] = {
        # ── Android Phones ─────────────────────────────────
        "Pixel 7": MobileDevice(
            name            = "Pixel 7",
            user_agent      = (
                "Mozilla/5.0 (Linux; Android 13; Pixel 7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.6422.142 Mobile Safari/537.36"
            ),
            screen_width    = 1080,
            screen_height   = 2400,
            viewport_width  = 393,
            viewport_height = 851,
            pixel_ratio     = 2.75,
            touch_points    = 5,
            os_family       = "Android",
            os_version      = "13",
            browser_version = "125.0.6422.142",
        ),
        "Pixel 8": MobileDevice(
            name            = "Pixel 8",
            user_agent      = (
                "Mozilla/5.0 (Linux; Android 14; Pixel 8) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.6422.142 Mobile Safari/537.36"
            ),
            screen_width    = 1080,
            screen_height   = 2400,
            viewport_width  = 393,
            viewport_height = 851,
            pixel_ratio     = 2.75,
            touch_points    = 5,
            os_family       = "Android",
            os_version      = "14",
            browser_version = "125.0.6422.142",
        ),
        "Samsung Galaxy S24": MobileDevice(
            name            = "Samsung Galaxy S24",
            user_agent      = (
                "Mozilla/5.0 (Linux; Android 14; SM-S921B) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.6422.142 Mobile Safari/537.36"
            ),
            screen_width    = 1080,
            screen_height   = 2340,
            viewport_width  = 360,
            viewport_height = 780,
            pixel_ratio     = 3.0,
            touch_points    = 5,
            os_family       = "Android",
            os_version      = "14",
            browser_version = "125.0.6422.142",
        ),
        "Samsung Galaxy S23": MobileDevice(
            name            = "Samsung Galaxy S23",
            user_agent      = (
                "Mozilla/5.0 (Linux; Android 13; SM-S911B) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.6367.82 Mobile Safari/537.36"
            ),
            screen_width    = 1080,
            screen_height   = 2340,
            viewport_width  = 360,
            viewport_height = 780,
            pixel_ratio     = 3.0,
            touch_points    = 5,
            os_family       = "Android",
            os_version      = "13",
            browser_version = "124.0.6367.82",
        ),
        "OnePlus 12": MobileDevice(
            name            = "OnePlus 12",
            user_agent      = (
                "Mozilla/5.0 (Linux; Android 14; CPH2583) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.6422.142 Mobile Safari/537.36"
            ),
            screen_width    = 1440,
            screen_height   = 3168,
            viewport_width  = 412,
            viewport_height = 915,
            pixel_ratio     = 3.5,
            touch_points    = 5,
            os_family       = "Android",
            os_version      = "14",
            browser_version = "125.0.6422.142",
        ),
        # ── iPhones ────────────────────────────────────────
        "iPhone 15 Pro": MobileDevice(
            name            = "iPhone 15 Pro",
            user_agent      = (
                "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                "Version/17.4.1 Mobile/15E148 Safari/604.1"
            ),
            screen_width    = 1179,
            screen_height   = 2556,
            viewport_width  = 393,
            viewport_height = 852,
            pixel_ratio     = 3.0,
            touch_points    = 5,
            os_family       = "iOS",
            os_version      = "17.4",
            browser_version = "17.4.1",
        ),
        "iPhone 15": MobileDevice(
            name            = "iPhone 15",
            user_agent      = (
                "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                "Version/17.4 Mobile/15E148 Safari/604.1"
            ),
            screen_width    = 1179,
            screen_height   = 2556,
            viewport_width  = 390,
            viewport_height = 844,
            pixel_ratio     = 3.0,
            touch_points    = 5,
            os_family       = "iOS",
            os_version      = "17.4",
            browser_version = "17.4",
        ),
        "iPhone 14": MobileDevice(
            name            = "iPhone 14",
            user_agent      = (
                "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                "Version/16.6 Mobile/15E148 Safari/604.1"
            ),
            screen_width    = 1170,
            screen_height   = 2532,
            viewport_width  = 390,
            viewport_height = 844,
            pixel_ratio     = 3.0,
            touch_points    = 5,
            os_family       = "iOS",
            os_version      = "16.6",
            browser_version = "16.6",
        ),
        # ── Tablets ────────────────────────────────────────
        "iPad Pro 12.9": MobileDevice(
            name            = "iPad Pro 12.9",
            user_agent      = (
                "Mozilla/5.0 (iPad; CPU OS 17_4 like Mac OS X) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                "Version/17.4 Mobile/15E148 Safari/604.1"
            ),
            screen_width    = 2048,
            screen_height   = 2732,
            viewport_width  = 1024,
            viewport_height = 1366,
            pixel_ratio     = 2.0,
            touch_points    = 5,
            os_family       = "iOS",
            os_version      = "17.4",
            browser_version = "17.4",
            is_tablet       = True,
        ),
        "Samsung Galaxy Tab S9": MobileDevice(
            name            = "Samsung Galaxy Tab S9",
            user_agent      = (
                "Mozilla/5.0 (Linux; Android 13; SM-X710) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.6422.142 Safari/537.36"
            ),
            screen_width    = 1600,
            screen_height   = 2560,
            viewport_width  = 800,
            viewport_height = 1280,
            pixel_ratio     = 2.0,
            touch_points    = 5,
            os_family       = "Android",
            os_version      = "13",
            browser_version = "125.0.6422.142",
            is_tablet       = True,
        ),
    }

    # Device weights (popularity)
    DEVICE_WEIGHTS = {
        "Pixel 7":              8.0,
        "Pixel 8":              7.0,
        "Samsung Galaxy S24":   12.0,
        "Samsung Galaxy S23":   10.0,
        "OnePlus 12":           5.0,
        "iPhone 15 Pro":        9.0,
        "iPhone 15":            11.0,
        "iPhone 14":            8.0,
        "iPad Pro 12.9":        3.0,
        "Samsung Galaxy Tab S9": 2.0,
    }

    @classmethod
    def get(cls, name: str) -> Optional[MobileDevice]:
        return cls.DEVICES.get(name)

    @classmethod
    def get_random(
        cls,
        os_family:  Optional[str]   = None,
        tablet:     Optional[bool]  = None,
    ) -> MobileDevice:
        """Get a random device weighted by popularity."""
        devices = list(cls.DEVICES.values())

        if os_family:
            devices = [
                d for d in devices
                if d.os_family.lower() == os_family.lower()
            ]
        if tablet is not None:
            devices = [
                d for d in devices
                if d.is_tablet == tablet
            ]

        if not devices:
            devices = list(cls.DEVICES.values())

        weights = [
            cls.DEVICE_WEIGHTS.get(d.name, 1.0)
            for d in devices
        ]
        total = sum(weights)
        r = random.uniform(0, total)
        cumulative = 0.0
        for device, weight in zip(devices, weights):
            cumulative += weight
            if r <= cumulative:
                return device
        return devices[-1]

    @classmethod
    def get_all_names(cls) -> List[str]:
        return list(cls.DEVICES.keys())


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Mobile Emulator
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class MobileEmulator:
    """
    Complete mobile device emulation for Chrome.

    Features:
    ─────────────────────────────────────────────────────
    • Real device profiles (10 devices)
    • CDP-based viewport and touch emulation
    • Sensor simulation (gyroscope, accelerometer)
    • Orientation change simulation
    • Battery API simulation
    • Touch event generation
    • Geolocation spoofing
    • Network condition simulation (4G/WiFi)
    """

    def __init__(
        self,
        driver:     Any,
        device:     Optional[MobileDevice] = None,
    ):
        self._driver        = driver
        self._device        = device or MobileDeviceDB.get_random()
        self._db            = MobileDeviceDB()
        self._applied       = False
        self._orientation   = "portrait-primary"

        logger.debug(
            f"[MobileEmulator] Device: {self._device.name} | "
            f"{self._device.viewport_width}x{self._device.viewport_height}"
        )

    # ── Setup ──────────────────────────────────────────────

    async def apply(self) -> bool:
        """Apply complete mobile emulation to browser."""
        try:
            loop = asyncio.get_event_loop()

            # Set device metrics via CDP
            await loop.run_in_executor(
                None,
                lambda: self._driver.execute_cdp_cmd(
                    "Emulation.setDeviceMetricsOverride",
                    {
                        "width":            self._device.viewport_width,
                        "height":           self._device.viewport_height,
                        "deviceScaleFactor": self._device.pixel_ratio,
                        "mobile":           True,
                        "screenWidth":      self._device.screen_width,
                        "screenHeight":     self._device.screen_height,
                        "positionX":        0,
                        "positionY":        0,
                    },
                ),
            )

            # Set touch emulation
            await loop.run_in_executor(
                None,
                lambda: self._driver.execute_cdp_cmd(
                    "Emulation.setTouchEmulationEnabled",
                    {
                        "enabled":      True,
                        "maxTouchPoints": self._device.touch_points,
                    },
                ),
            )

            # Set user agent
            await loop.run_in_executor(
                None,
                lambda: self._driver.execute_cdp_cmd(
                    "Network.setUserAgentOverride",
                    {
                        "userAgent":    self._device.user_agent,
                        "platform":     (
                            "iPhone" if self._device.os_family == "iOS"
                            else "Linux armv8l"
                        ),
                    },
                ),
            )

            # Inject mobile-specific JavaScript
            await self._inject_mobile_apis()

            self._applied = True
            logger.info(
                f"[MobileEmulator] Applied: {self._device.name}"
            )
            return True

        except Exception as exc:
            logger.error(f"[MobileEmulator] Apply error: {exc}")
            return False

    async def _inject_mobile_apis(self) -> None:
        """Inject mobile-specific browser APIs."""
        device      = self._device
        battery_lvl = device.battery_level + random.uniform(-0.05, 0.05)
        battery_lvl = max(0.1, min(1.0, battery_lvl))

        script = f"""
        (function() {{
            // Touch support
            Object.defineProperty(navigator, 'maxTouchPoints', {{
                get: () => {device.touch_points},
                configurable: true,
            }});

            // Battery API
            const mockBattery = {{
                charging:        {str(battery_lvl > 0.9).lower()},
                chargingTime:    {0 if battery_lvl > 0.9 else int((1 - battery_lvl) * 7200)},
                dischargingTime: {int(battery_lvl * 14400)},
                level:           {battery_lvl:.2f},
                addEventListener:    () => {{}},
                removeEventListener: () => {{}},
                dispatchEvent:       () => true,
            }};
            if (navigator.getBattery) {{
                navigator.getBattery = () => Promise.resolve(mockBattery);
            }}

            // Device orientation
            window.DeviceOrientationEvent = window.DeviceOrientationEvent || class {{}};
            window.DeviceMotionEvent = window.DeviceMotionEvent || class {{}};

            // Connection type
            if (navigator.connection) {{
                Object.defineProperty(navigator.connection, 'type', {{
                    get: () => '4g',
                    configurable: true,
                }});
                Object.defineProperty(navigator.connection, 'effectiveType', {{
                    get: () => '4g',
                    configurable: true,
                }});
                Object.defineProperty(navigator.connection, 'downlink', {{
                    get: () => {random.uniform(10, 50):.1f},
                    configurable: true,
                }});
            }}

            // Screen orientation
            if (screen.orientation) {{
                Object.defineProperty(screen.orientation, 'type', {{
                    get: () => '{device.orientation}',
                    configurable: true,
                }});
                Object.defineProperty(screen.orientation, 'angle', {{
                    get: () => 0,
                    configurable: true,
                }});
            }}

            // Vibration API (mobile-only)
            navigator.vibrate = navigator.vibrate || function(pattern) {{
                return true;
            }};
        }})();
        """

        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None,
                lambda: self._driver.execute_cdp_cmd(
                    "Page.addScriptToEvaluateOnNewDocument",
                    {"source": script},
                ),
            )
            await loop.run_in_executor(
                None,
                lambda: self._driver.execute_script(script),
            )
        except Exception as exc:
            logger.debug(f"[MobileEmulator] API inject error: {exc}")

    # ── Interaction ────────────────────────────────────────

    async def simulate_orientation_change(
        self,
        orientation: str = "landscape-primary",
    ) -> None:
        """Simulate device orientation change."""
        try:
            loop   = asyncio.get_event_loop()
            device = self._device

            if orientation == "landscape-primary":
                new_w, new_h = device.viewport_height, device.viewport_width
                angle = 90
            else:
                new_w, new_h = device.viewport_width, device.viewport_height
                angle = 0

            await loop.run_in_executor(
                None,
                lambda: self._driver.execute_cdp_cmd(
                    "Emulation.setDeviceMetricsOverride",
                    {
                        "width":            new_w,
                        "height":           new_h,
                        "deviceScaleFactor": device.pixel_ratio,
                        "mobile":           True,
                        "screenOrientation": {
                            "type":  orientation,
                            "angle": angle,
                        },
                    },
                ),
            )

            # Dispatch orientation change event
            await loop.run_in_executor(
                None,
                lambda: self._driver.execute_script(
                    f"""
                    window.dispatchEvent(new Event('orientationchange'));
                    screen.orientation && screen.orientation.dispatchEvent(
                        new Event('change')
                    );
                    """
                ),
            )
            self._orientation = orientation
            logger.debug(
                f"[MobileEmulator] Orientation: {orientation}"
            )
        except Exception as exc:
            logger.debug(
                f"[MobileEmulator] Orientation error: {exc}"
            )

    async def set_geolocation(
        self,
        latitude:   float,
        longitude:  float,
        accuracy:   float = 10.0,
    ) -> bool:
        """Set device geolocation via CDP."""
        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None,
                lambda: self._driver.execute_cdp_cmd(
                    "Emulation.setGeolocationOverride",
                    {
                        "latitude":     latitude,
                        "longitude":    longitude,
                        "accuracy":     accuracy,
                    },
                ),
            )
            return True
        except Exception as exc:
            logger.debug(f"[MobileEmulator] Geolocation error: {exc}")
            return False

    async def simulate_touch(
        self,
        x:          float,
        y:          float,
        duration_ms: float = 100.0,
    ) -> None:
        """Simulate a touch event at coordinates."""
        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None,
                lambda: self._driver.execute_cdp_cmd(
                    "Input.dispatchTouchEvent",
                    {
                        "type": "touchStart",
                        "touchPoints": [{
                            "x": int(x),
                            "y": int(y),
                            "id": 0,
                        }],
                    },
                ),
            )
            await asyncio.sleep(duration_ms / 1000)
            await loop.run_in_executor(
                None,
                lambda: self._driver.execute_cdp_cmd(
                    "Input.dispatchTouchEvent",
                    {
                        "type": "touchEnd",
                        "touchPoints": [],
                    },
                ),
            )
        except Exception as exc:
            logger.debug(f"[MobileEmulator] Touch error: {exc}")

    async def simulate_swipe(
        self,
        start_x:    float,
        start_y:    float,
        end_x:      float,
        end_y:      float,
        duration_ms: float = 300.0,
        steps:      int   = 10,
    ) -> None:
        """Simulate a swipe gesture."""
        try:
            loop  = asyncio.get_event_loop()
            delay = duration_ms / steps / 1000

            # Touch start
            await loop.run_in_executor(
                None,
                lambda: self._driver.execute_cdp_cmd(
                    "Input.dispatchTouchEvent",
                    {
                        "type": "touchStart",
                        "touchPoints": [{
                            "x": int(start_x),
                            "y": int(start_y),
                            "id": 0,
                        }],
                    },
                ),
            )

            # Touch move
            for i in range(1, steps + 1):
                t = i / steps
                cx = start_x + (end_x - start_x) * t
                cy = start_y + (end_y - start_y) * t

                await loop.run_in_executor(
                    None,
                    lambda cx=cx, cy=cy: self._driver.execute_cdp_cmd(
                        "Input.dispatchTouchEvent",
                        {
                            "type": "touchMove",
                            "touchPoints": [{
                                "x": int(cx),
                                "y": int(cy),
                                "id": 0,
                            }],
                        },
                    ),
                )
                await asyncio.sleep(delay)

            # Touch end
            await loop.run_in_executor(
                None,
                lambda: self._driver.execute_cdp_cmd(
                    "Input.dispatchTouchEvent",
                    {
                        "type": "touchEnd",
                        "touchPoints": [],
                    },
                ),
            )

        except Exception as exc:
            logger.debug(f"[MobileEmulator] Swipe error: {exc}")

    async def reset(self) -> None:
        """Reset device emulation."""
        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None,
                lambda: self._driver.execute_cdp_cmd(
                    "Emulation.clearDeviceMetricsOverride", {}
                ),
            )
            await loop.run_in_executor(
                None,
                lambda: self._driver.execute_cdp_cmd(
                    "Emulation.setTouchEmulationEnabled",
                    {"enabled": False},
                ),
            )
            self._applied = False
        except Exception:
            pass

    # ── Properties ─────────────────────────────────────────

    @property
    def device(self) -> MobileDevice:
        return self._device

    @property
    def is_applied(self) -> bool:
        return self._applied

    def switch_device(self, device: MobileDevice) -> None:
        """Switch to a different device profile."""
        self._device  = device
        self._applied = False
        logger.debug(f"[MobileEmulator] Switched to: {device.name}")

    def get_stats(self) -> Dict[str, Any]:
        return {
            "device":       self._device.name,
            "viewport":     f"{self._device.viewport_width}x{self._device.viewport_height}",
            "pixel_ratio":  self._device.pixel_ratio,
            "os":           self._device.os_family,
            "is_applied":   self._applied,
            "orientation":  self._orientation,
        }