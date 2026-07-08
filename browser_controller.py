"""
Jubra Traffic Pro - Browser Controller (nodriver Edition)
Next-gen Chrome orchestration using nodriver.
No chromedriver needed. Native CDP. Async-first.
"""

import asyncio
import time
import uuid
import json
import random
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from enum import Enum, auto

try:
    import nodriver as uc
    HAS_NODRIVER = True
except ImportError:
    HAS_NODRIVER = False
    logging.warning("[BrowserController] nodriver not installed")

try:
    import psutil
    HAS_PSUTIL = True
except ImportError:
    HAS_PSUTIL = False

from core.exceptions import (
    BrowserError,
    BrowserLaunchError,
    BrowserCrashedError,
    PageLoadError,
    CDPInjectionError,
    ErrorContext,
)
from core.config_manager import ConfigManager

logger = logging.getLogger(__name__)


class BrowserState(Enum):
    INITIALIZING = auto()
    READY        = auto()
    NAVIGATING   = auto()
    EXECUTING    = auto()
    CRASHED      = auto()
    RECYCLING    = auto()
    DESTROYED    = auto()


@dataclass
class BrowserProfile:
    """Complete browser configuration profile."""
    profile_id:           str
    user_agent:           str
    viewport_width:       int
    viewport_height:      int
    color_depth:          int            = 24
    pixel_ratio:          float          = 1.0
    language:             str            = "en-US"
    languages:            List[str]      = field(
        default_factory=lambda: ["en-US", "en"]
    )
    timezone:             str            = "America/New_York"
    platform:             str            = "Win32"
    hardware_concurrency: int            = 8
    device_memory:        int            = 8
    max_touch_points:     int            = 0
    webgl_vendor:         str            = "Google Inc. (NVIDIA)"
    webgl_renderer:       str            = "ANGLE (NVIDIA, GeForce RTX 3060)"
    canvas_noise_seed:    int            = 0
    audio_noise_seed:     int            = 0
    proxy_url:            Optional[str]  = None
    headless:             bool           = True
    disable_images:       bool           = False
    extra_args:           List[str]      = field(default_factory=list)
    is_mobile:            bool           = False

    def __post_init__(self):
        if self.canvas_noise_seed == 0:
            self.canvas_noise_seed = random.randint(1, 2**31 - 1)
        if self.audio_noise_seed == 0:
            self.audio_noise_seed = random.randint(1, 2**31 - 1)


class StealthScripts:
    """All anti-detection JavaScript injection scripts."""

    @staticmethod
    def navigator_override(profile: BrowserProfile) -> str:
        langs_json = json.dumps(profile.languages)
        return f"""
        (function() {{
            const overrides = {{
                userAgent:           '{profile.user_agent}',
                platform:            '{profile.platform}',
                language:            '{profile.language}',
                languages:           {langs_json},
                hardwareConcurrency: {profile.hardware_concurrency},
                deviceMemory:        {profile.device_memory},
                maxTouchPoints:      {profile.max_touch_points},
                vendor:              'Google Inc.',
            }};
            for (const [key, val] of Object.entries(overrides)) {{
                Object.defineProperty(navigator, key, {{
                    get: () => val, configurable: true
                }});
            }}
            Object.defineProperty(navigator, 'webdriver', {{
                get: () => undefined, configurable: true
            }});
        }})();
        """

    @staticmethod
    def canvas_spoof(seed: int) -> str:
        return f"""
        (function() {{
            const SEED = {seed};
            function prng(s) {{
                return function() {{
                    s |= 0; s = s + 0x6D2B79F5 | 0;
                    let t = Math.imul(s ^ s >>> 15, 1 | s);
                    t = t + Math.imul(t ^ t >>> 7, 61 | t) ^ t;
                    return ((t ^ t >>> 14) >>> 0) / 4294967296;
                }};
            }}
            const rng = prng(SEED);
            const origToDataURL = HTMLCanvasElement.prototype.toDataURL;
            const origGetImageData = CanvasRenderingContext2D
                .prototype.getImageData;

            function addNoise(canvas) {{
                try {{
                    const ctx = canvas.getContext('2d');
                    if (!ctx) return;
                    const img = origGetImageData.call(
                        ctx, 0, 0, canvas.width, canvas.height
                    );
                    for (let i = 0; i < img.data.length; i += 4) {{
                        const n = Math.floor((rng() - 0.5) * 4);
                        img.data[i]   = Math.max(
                            0, Math.min(255, img.data[i] + n)
                        );
                        img.data[i+1] = Math.max(
                            0, Math.min(255, img.data[i+1] + n)
                        );
                        img.data[i+2] = Math.max(
                            0, Math.min(255, img.data[i+2] + n)
                        );
                    }}
                    ctx.putImageData(img, 0, 0);
                }} catch(e) {{}}
            }}

            HTMLCanvasElement.prototype.toDataURL = function(...a) {{
                addNoise(this);
                return origToDataURL.apply(this, a);
            }};
        }})();
        """

    @staticmethod
    def webgl_spoof(vendor: str, renderer: str) -> str:
        return f"""
        (function() {{
            const origGetParam =
                WebGLRenderingContext.prototype.getParameter;
            WebGLRenderingContext.prototype.getParameter = function(p) {{
                if (p === 0x9245 || p === 0x1F00) return '{vendor}';
                if (p === 0x9246 || p === 0x1F01) return '{renderer}';
                return origGetParam.call(this, p);
            }};
            if (typeof WebGL2RenderingContext !== 'undefined') {{
                const orig2 =
                    WebGL2RenderingContext.prototype.getParameter;
                WebGL2RenderingContext.prototype.getParameter =
                    function(p) {{
                    if (p === 0x9245 || p === 0x1F00)
                        return '{vendor}';
                    if (p === 0x9246 || p === 0x1F01)
                        return '{renderer}';
                    return orig2.call(this, p);
                }};
            }}
        }})();
        """

    @staticmethod
    def audio_spoof(seed: int) -> str:
        return f"""
        (function() {{
            const SEED = {seed};
            function noise(s, i) {{
                const x = Math.sin(s + i * 127.1) * 43758.5453;
                return x - Math.floor(x);
            }}
            const orig = AudioBuffer.prototype.getChannelData;
            AudioBuffer.prototype.getChannelData = function(ch) {{
                const data = orig.call(this, ch);
                for (let i = 0; i < data.length; i++) {{
                    data[i] += (noise(SEED, i) - 0.5) * 1e-7;
                }}
                return data;
            }};
        }})();
        """

    @staticmethod
    def timezone_override(timezone: str) -> str:
        return f"""
        (function() {{
            const tz = '{timezone}';
            const origDTF = Intl.DateTimeFormat;
            Intl.DateTimeFormat = function(locale, opts) {{
                opts = opts || {{}};
                opts.timeZone = opts.timeZone || tz;
                return new origDTF(locale, opts);
            }};
            Intl.DateTimeFormat.prototype =
                origDTF.prototype;
            Intl.DateTimeFormat.supportedLocalesOf =
                origDTF.supportedLocalesOf;
        }})();
        """

    @staticmethod
    def screen_override(
        w: int, h: int, depth: int, ratio: float
    ) -> str:
        return f"""
        (function() {{
            const props = {{
                width: {w}, height: {h},
                availWidth: {w}, availHeight: {h} - 40,
                colorDepth: {depth}, pixelDepth: {depth}
            }};
            for (const [k, v] of Object.entries(props)) {{
                Object.defineProperty(screen, k, {{
                    get: () => v
                }});
            }}
            Object.defineProperty(window, 'devicePixelRatio', {{
                get: () => {ratio}
            }});
        }})();
        """

    @staticmethod
    def chrome_runtime() -> str:
        return """
        (function() {
            if (!window.chrome) window.chrome = {};
            if (!window.chrome.runtime) {
                window.chrome.runtime = {
                    onConnect: { addListener: () => {} },
                    onMessage: { addListener: () => {} },
                    connect:     () => ({}),
                    sendMessage: () => {},
                    id:          undefined,
                };
            }
            for (const key of Object.keys(window)) {
                if (
                    key.startsWith('cdc_') ||
                    key.startsWith('$chrome_')
                ) {
                    try { delete window[key]; } catch(e) {}
                }
            }
        })();
        """

    @staticmethod
    def plugins_spoof() -> str:
        return """
        (function() {
            Object.defineProperty(navigator, 'plugins', {
                get: () => {
                    return [
                        { name: 'Chrome PDF Plugin', length: 1 },
                        { name: 'Chrome PDF Viewer', length: 1 },
                        { name: 'Native Client',    length: 2 },
                    ];
                }
            });
        })();
        """

    @classmethod
    def get_all(cls, profile: BrowserProfile) -> str:
        """Combine all stealth scripts into one."""
        scripts = [
            cls.chrome_runtime(),
            cls.navigator_override(profile),
            cls.screen_override(
                profile.viewport_width,
                profile.viewport_height,
                profile.color_depth,
                profile.pixel_ratio,
            ),
            cls.webgl_spoof(
                profile.webgl_vendor,
                profile.webgl_renderer,
            ),
            cls.canvas_spoof(profile.canvas_noise_seed),
            cls.audio_spoof(profile.audio_noise_seed),
            cls.timezone_override(profile.timezone),
            cls.plugins_spoof(),
        ]
        return "\n".join(scripts)


class BrowserInstance:
    """
    Managed Chrome browser using nodriver.
    No chromedriver needed. Native async CDP.
    """

    def __init__(
        self,
        browser_id:    str,
        profile:       BrowserProfile,
        config:        ConfigManager,
        recycle_after: int = 50,
    ):
        self.browser_id    = browser_id
        self.profile       = profile
        self._config       = config
        self._recycle_after = recycle_after

        self._browser:     Optional[Any]  = None
        self._page:        Optional[Any]  = None
        self._state        = BrowserState.INITIALIZING

        self._pages_loaded: int           = 0
        self._errors:       int           = 0
        self._created_at:   float         = time.monotonic()
        self._last_used:    float         = time.monotonic()
        self._session_id:   Optional[str] = None
        self._pid:          Optional[int] = None
        self._memory_mb:    float         = 0.0
        self._cpu_pct:      float         = 0.0

        logger.debug(
            f"[BrowserInstance] Created: {browser_id} | "
            f"nodriver | headless={profile.headless}"
        )

    async def launch(self) -> None:
        """Launch Chrome browser using nodriver."""
        if not HAS_NODRIVER:
            raise BrowserLaunchError(
                reason="nodriver not installed. pip install nodriver",
                context=ErrorContext(
                    module="BrowserInstance",
                    browser_id=self.browser_id,
                ),
            )

        try:
            browser_args = self._build_args()

            self._browser = await uc.start(
                headless=self.profile.headless,
                browser_args=browser_args,
                lang=self.profile.language,
            )

            # nodriver 0.50.3 এ tabs list থেকে tab নিতে হয়
            await asyncio.sleep(1.5)
            tabs = self._browser.tabs

            if tabs:
                self._page = tabs[0]
            else:
                self._page = self._browser.main_tab

            if self._page is None:
                raise Exception("No browser tab available")

            # page fully ready হওয়ার জন্য একটু wait
            await asyncio.sleep(0.5)

            try:
                if (
                    hasattr(self._browser, "_process")
                    and self._browser._process
                ):
                    self._pid = self._browser._process.pid
            except Exception:
                pass

            await self._inject_stealth()
            await self._set_viewport()

            self._state = BrowserState.READY
            logger.info(
                f"[BrowserInstance] Launched (nodriver): "
                f"{self.browser_id} | pid={self._pid}"
            )

        except Exception as exc:
            self._state = BrowserState.CRASHED
            raise BrowserLaunchError(
                reason=str(exc),
                context=ErrorContext(
                    module="BrowserInstance",
                    browser_id=self.browser_id,
                ),
            ) from exc

    def _build_args(self) -> List[str]:
        """Build Chrome launch arguments."""
        args = [
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
            "--disable-infobars",
            "--disable-extensions",
            "--disable-popup-blocking",
            "--disable-background-timer-throttling",
            "--disable-backgrounding-occluded-windows",
            "--disable-renderer-backgrounding",
            "--no-first-run",
            "--no-service-autorun",
            "--password-store=basic",
            "--disable-gpu",
            "--ignore-certificate-errors",
            "--ignore-ssl-errors",
            f"--lang={self.profile.language}",
            f"--window-size={self.profile.viewport_width},"
            f"{self.profile.viewport_height}",
        ]

        if self.profile.proxy_url:
            args.append(
                f"--proxy-server={self.profile.proxy_url}"
            )

        if self.profile.disable_images:
            args.append(
                "--blink-settings=imagesEnabled=false"
            )

        args.extend(self.profile.extra_args)
        return args

    async def _inject_stealth(self) -> None:
        """Inject anti-detection scripts via CDP."""
        if not self._page:
            return

        try:
            combined = StealthScripts.get_all(self.profile)
            await self._page.evaluate(combined)
            logger.debug(
                f"[BrowserInstance] Stealth injected: "
                f"{self.browser_id}"
            )
        except Exception as exc:
            logger.error(
                f"[BrowserInstance] Stealth injection failed: "
                f"{exc}"
            )

    async def _set_viewport(self) -> None:
        """Set viewport size via CDP."""
        if not self._page:
            return

        try:
            await self._page.send(
                uc.cdp.emulation.set_device_metrics_override(
                    width=self.profile.viewport_width,
                    height=self.profile.viewport_height,
                    device_scale_factor=self.profile.pixel_ratio,
                    mobile=self.profile.is_mobile,
                )
            )
        except Exception as exc:
            logger.debug(
                f"[BrowserInstance] Viewport error: {exc}"
            )

    async def navigate(
        self,
        url:            str,
        wait_condition: str    = "domcontentloaded",
        timeout:        float  = 30.0,
        retry_count:    int    = 2,
    ) -> bool:
        """Navigate to URL with retry logic."""
        if not self._page:
            raise PageLoadError(url=url, status_code=0)

        self._state    = BrowserState.NAVIGATING
        self._last_used = time.monotonic()

        for attempt in range(retry_count + 1):
            try:
                start = time.monotonic()

                self._page = await self._browser.get(
                    url, new_tab=False,
                )

                if wait_condition == "domcontentloaded":
                    await self._page.sleep(
                        random.uniform(1.0, 2.0)
                    )
                elif wait_condition == "networkidle":
                    await self._page.sleep(
                        random.uniform(2.0, 4.0)
                    )
                elif wait_condition == "load":
                    await self._page.sleep(
                        random.uniform(1.5, 3.0)
                    )

                load_time = (time.monotonic() - start) * 1000
                self._pages_loaded += 1
                self._state = BrowserState.READY

                logger.debug(
                    f"[BrowserInstance] Navigated: "
                    f"{url[:60]} | load={load_time:.0f}ms"
                )
                return True

            except asyncio.TimeoutError:
                if attempt < retry_count:
                    await asyncio.sleep(2.0)
                else:
                    self._errors += 1
                    self._state = BrowserState.READY
                    return False

            except Exception as exc:
                if attempt < retry_count:
                    await asyncio.sleep(1.0)
                else:
                    self._errors += 1
                    self._state = BrowserState.READY
                    logger.debug(
                        f"[BrowserInstance] Nav error: {exc}"
                    )
                    return False

        return False

    async def execute_script(
        self, script: str, *args
    ) -> Any:
        """Execute JavaScript."""
        if not self._page:
            return None
        try:
            self._state = BrowserState.EXECUTING
            result = await self._page.evaluate(script)
            self._state = BrowserState.READY
            return result
        except Exception as exc:
            self._state = BrowserState.READY
            logger.debug(
                f"[BrowserInstance] JS error: {exc}"
            )
            return None

    async def find_element(
        self,
        selector: str,
        timeout:  float = 10.0,
    ) -> Optional[Any]:
        """Find element with wait."""
        if not self._page:
            return None
        try:
            element = await self._page.select(
                selector, timeout=timeout,
            )
            return element
        except Exception:
            return None

    async def find_elements(
        self, selector: str
    ) -> List[Any]:
        """Find all matching elements."""
        if not self._page:
            return []
        try:
            elements = await self._page.select_all(selector)
            return elements or []
        except Exception:
            return []

    async def click_element(self, element: Any) -> bool:
        """Click an element."""
        if not element:
            return False
        try:
            await element.click()
            return True
        except Exception as exc:
            logger.debug(
                f"[BrowserInstance] Click error: {exc}"
            )
            return False

    async def type_text(
        self,
        element:  Any,
        text:     str,
        delay_ms: Tuple[int, int] = (80, 200),
    ) -> bool:
        """Type text into element."""
        if not element:
            return False
        try:
            await element.clear_input()
            await asyncio.sleep(random.uniform(0.1, 0.3))
            for char in text:
                await element.send_keys(char)
                delay = random.randint(*delay_ms) / 1000
                await asyncio.sleep(delay)
            return True
        except Exception as exc:
            logger.debug(
                f"[BrowserInstance] Type error: {exc}"
            )
            return False

    async def get_current_url(self) -> str:
        if not self._page:
            return ""
        try:
            return self._page.url or ""
        except Exception:
            return ""

    async def get_page_source(self) -> str:
        if not self._page:
            return ""
        try:
            source = await self._page.get_content()
            return source or ""
        except Exception:
            return ""

    async def get_title(self) -> str:
        if not self._page:
            return ""
        try:
            title = await self._page.evaluate(
                "document.title"
            )
            return title or ""
        except Exception:
            return ""

    async def take_screenshot(
        self, path: Optional[str] = None
    ) -> Optional[bytes]:
        """Take screenshot."""
        if not self._page:
            return None
        try:
            if path:
                await self._page.save_screenshot(path)
                return None
            else:
                return await self._page.get_screenshot()
        except Exception:
            return None

    async def get_cookies(self) -> List[Dict[str, Any]]:
        if not self._page:
            return []
        try:
            cookies = await self._page.send(
                uc.cdp.network.get_all_cookies()
            )
            return [
                {
                    "name":   c.name,
                    "value":  c.value,
                    "domain": c.domain,
                    "path":   c.path,
                }
                for c in cookies
            ]
        except Exception:
            return []

    async def add_cookie(
        self, cookie: Dict[str, Any]
    ) -> None:
        try:
            await self._page.send(
                uc.cdp.network.set_cookie(
                    name=cookie.get("name", ""),
                    value=cookie.get("value", ""),
                    domain=cookie.get("domain", ""),
                    path=cookie.get("path", "/"),
                )
            )
        except Exception as exc:
            logger.debug(
                f"[BrowserInstance] Cookie error: {exc}"
            )

    async def clear_cookies(self) -> None:
        try:
            await self._page.send(
                uc.cdp.network.clear_browser_cookies()
            )
        except Exception:
            pass

    async def get_resource_usage(self) -> Dict[str, float]:
        """Get CPU and memory usage."""
        if not HAS_PSUTIL or not self._pid:
            return {"cpu_pct": 0.0, "memory_mb": 0.0}
        try:
            proc = psutil.Process(self._pid)
            self._cpu_pct = proc.cpu_percent(interval=0.1)
            self._memory_mb = (
                proc.memory_info().rss / 1024 / 1024
            )
            return {
                "cpu_pct":   self._cpu_pct,
                "memory_mb": self._memory_mb,
            }
        except (psutil.NoSuchProcess, Exception):
            return {"cpu_pct": 0.0, "memory_mb": 0.0}

    @property
    def state(self) -> BrowserState:
        return self._state

    @property
    def is_ready(self) -> bool:
        return (
            self._state == BrowserState.READY
            and self._browser is not None
        )

    @property
    def is_crashed(self) -> bool:
        return self._state == BrowserState.CRASHED

    @property
    def needs_recycling(self) -> bool:
        return self._pages_loaded >= self._recycle_after

    @property
    def uptime(self) -> float:
        return time.monotonic() - self._created_at

    @property
    def pages_loaded(self) -> int:
        return self._pages_loaded

    @property
    def driver(self) -> Optional[Any]:
        """Compatibility - returns page object."""
        return self._page

    def bind_session(self, session_id: str) -> None:
        self._session_id = session_id

    def unbind_session(self) -> Optional[str]:
        sid = self._session_id
        self._session_id = None
        return sid

    @property
    def bound_session(self) -> Optional[str]:
        return self._session_id

    async def recycle(self) -> None:
        """Reset browser for reuse."""
        self._state = BrowserState.RECYCLING
        try:
            await self.clear_cookies()
            await self.execute_script(
                "window.localStorage.clear(); "
                "window.sessionStorage.clear();"
            )

            if self._browser:
                self._page = await self._browser.get(
                    "about:blank"
                )

            self._session_id   = None
            self._pages_loaded = 0

            await self._inject_stealth()

            self._state = BrowserState.READY
            logger.debug(
                f"[BrowserInstance] Recycled: "
                f"{self.browser_id}"
            )
        except Exception as exc:
            self._state = BrowserState.CRASHED
            raise BrowserCrashedError(
                browser_id=self.browser_id,
                reason=f"Recycle failed: {exc}",
            )

    async def destroy(self) -> None:
        """Fully destroy browser instance."""
        self._state = BrowserState.DESTROYED
        try:
            if self._browser:
                self._browser.stop()
                self._browser = None
                self._page    = None
        except Exception as exc:
            logger.debug(
                f"[BrowserInstance] Destroy error: {exc}"
            )
        finally:
            logger.debug(
                f"[BrowserInstance] Destroyed: "
                f"{self.browser_id}"
            )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "browser_id":    self.browser_id,
            "state":         self._state.name,
            "engine":        "nodriver",
            "is_ready":      self.is_ready,
            "pages_loaded":  self._pages_loaded,
            "errors":        self._errors,
            "uptime":        round(self.uptime, 1),
            "memory_mb":     round(self._memory_mb, 1),
            "session_id":    self._session_id,
            "pid":           self._pid,
            "needs_recycle": self.needs_recycling,
        }