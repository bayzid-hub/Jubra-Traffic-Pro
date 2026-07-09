""" Jubra Traffic Pro - Browser Controller (nodriver Edition) Next-gen Chrome orchestration using nodriver. No chromedriver needed. Native CDP. Async-first. """

import asyncio
import time
import uuid
import json
import random
import logging
import shutil
import tempfile
import inspect
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
    READY = auto()
    NAVIGATING = auto()
    EXECUTING = auto()
    CRASHED = auto()
    RECYCLING = auto()
    DESTROYED = auto()

@dataclass
class BrowserProfile:
    """Complete browser configuration profile."""
    profile_id: str
    user_agent: str
    viewport_width: int
    viewport_height: int
    color_depth: int = 24
    pixel_ratio: float = 1.0
    language: str = "en-US"
    languages: List[str] = field(
        default_factory=lambda: ["en-US", "en"]
    )
    timezone: str = "America/New_York"
    platform: str = "Win32"
    hardware_concurrency: int = 8
    device_memory: int = 8
    max_touch_points: int = 0
    webgl_vendor: str = "Google Inc. (NVIDIA)"
    webgl_renderer: str = "ANGLE (NVIDIA, GeForce RTX 3060)"
    canvas_noise_seed: int = 0
    audio_noise_seed: int = 0
    proxy_url: Optional[str] = None
    headless: bool = True
    disable_images: bool = False
    extra_args: List[str] = field(default_factory=list)
    is_mobile: bool = False

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
        w: int, h: int, depth: int, ratio: float) -> str:
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
                    key.startsWith('\\$chrome_')
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
    Managed Chrome browser using nodriver. No chromedriver needed.
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
        self._profile_dir:  Optional[str] = None
        self._last_verified_url: str = ""
        self._last_final_url: str = ""
        self._last_url_source: str = ""
        self._memory_mb:    float         = 0.0
        self._cpu_pct:      float         = 0.0
        logger.debug(
            f"[BrowserInstance] Created: {browser_id} | "
            f"nodriver | headless={profile.headless}"
        )

    @staticmethod
    def _is_internal_error_url(url: str) -> bool:
        """Return True for browser-internal error pages that are not real loads."""
        value = (url or "").strip().lower()
        if not value:
            return False
        return (
            value.startswith("chrome-error://")
            or value.startswith("chrome://")
            or value.startswith("about:blank")
            or value.startswith("chrome-search://")
        )

    @staticmethod
    def _is_web_url(url: str) -> bool:
        value = (url or "").strip().lower()
        return value.startswith("http://") or value.startswith("https://")

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
            chrome_path = self._config.get("browser.chrome_path", None)

            # nodriver creates its own temporary user-data-dir when no profile
            # directory is supplied through the supported start() kwarg. Passing
            # a second --user-data-dir inside browser_args can create duplicate
            # Chrome profile arguments and visible blank launch flashes on
            # Windows. Prefer the official start() kwarg when available; keep a
            # browser_args fallback for older nodriver versions.
            try:
                _start_params = inspect.signature(uc.start).parameters
                _supports_user_data_dir = "user_data_dir" in _start_params
            except Exception:
                _supports_user_data_dir = False

            browser_args = self._build_args(
                include_user_data_dir=not _supports_user_data_dir
            )
            start_kwargs = {
                "headless": self.profile.headless,
                "browser_args": browser_args,
                "lang": self.profile.language,
            }
            if _supports_user_data_dir and self._profile_dir:
                start_kwargs["user_data_dir"] = self._profile_dir
            if chrome_path:
                start_kwargs["browser_executable_path"] = chrome_path
            self._browser = await uc.start(**start_kwargs)
            
            await asyncio.sleep(1.5)
            tabs = self._browser.tabs
            if tabs:
                self._page = tabs[0]
            else:
                self._page = self._browser.main_tab
            if self._page is None:
                raise Exception("No browser tab available")
                
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
            if self.profile.proxy_url and '@' in (self.profile.proxy_url or ''):
                await self._setup_proxy_auth()
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

    def _build_args(self, include_user_data_dir: bool = True) -> List[str]:
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
        if self._config.get("browser.force_background_launch", True):
            args.extend([
                "--disable-notifications",
                "--disable-default-apps",
                "--disable-sync",
                "--disable-background-networking",
            ])
        if self._config.get("browser.force_offscreen_launch", True):
            args.extend([
                "--window-position=-32000,-32000",
                "--start-minimized",
                "--disable-features=CalculateNativeWinOcclusion,TranslateUI",
            ])
        self._profile_dir = tempfile.mkdtemp(prefix="jtp_isolated_")
        if include_user_data_dir:
            args.append(f"--user-data-dir={self._profile_dir}")
        if self.profile.headless:
            args.append("--headless=new")
            args.append("--mute-audio")
            args.append("--disable-features=TranslateUI")
        if self.profile.proxy_url:
            from urllib.parse import urlparse as _urlparse
            _parsed = _urlparse(self.profile.proxy_url)
            if _parsed.username:
                _proxy_addr = f"{_parsed.scheme}://{_parsed.hostname}:{_parsed.port}"
            else:
                _proxy_addr = self.profile.proxy_url
            args.append(f"--proxy-server={_proxy_addr}")
        if self.profile.disable_images:
            args.append(
                "--blink-settings=imagesEnabled=false"
            )
        args.extend(self.profile.extra_args)
        return args

    async def _inject_stealth(self) -> None:
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

    async def _setup_proxy_auth(self) -> None:
        if not self._page or not self.profile.proxy_url:
            return
        try:
            from urllib.parse import urlparse, unquote
            parsed = urlparse(self.profile.proxy_url)
            if not parsed.username or not parsed.password:
                return
            username = unquote(parsed.username)
            password = unquote(parsed.password)
            await self._page.send(
                uc.cdp.fetch.enable(
                    patterns=[
                        uc.cdp.fetch.RequestPattern(url_pattern="*")
                    ],
                    handle_auth_requests=True,
                )
            )
            async def _on_auth_required(event: uc.cdp.fetch.AuthRequired):
                await self._page.send(
                    uc.cdp.fetch.continue_with_auth(
                        request_id=event.request_id,
                        auth_challenge_response=uc.cdp.fetch.AuthChallengeResponse(
                            response="ProvideCredentials",
                            username=username,
                            password=password,
                        ),
                    )
                )
            async def _on_request_paused(event: uc.cdp.fetch.RequestPaused):
                try:
                    await self._page.send(
                        uc.cdp.fetch.continue_request(
                            request_id=event.request_id,
                        )
                    )
                except Exception:
                    pass
            self._page.add_handler(
                uc.cdp.fetch.AuthRequired, _on_auth_required
            )
            self._page.add_handler(
                uc.cdp.fetch.RequestPaused, _on_request_paused
            )
            logger.info(
                f"[BrowserInstance] Proxy auth configured: "
                f"{parsed.hostname}:{parsed.port}"
            )
        except Exception as exc:
            logger.warning(
                f"[BrowserInstance] Proxy auth setup failed: {exc}"
            )

    async def _navigate_with_nodriver(self, url: str) -> None:
        """Navigate using the nodriver API available in the installed version."""
        if not self._page:
            raise RuntimeError("No active browser tab available")

        # Current nodriver Tab objects expose get(), not Selenium-style goto().
        # Keep fallbacks so this remains compatible across nodriver versions.
        if hasattr(self._page, "get"):
            result = await self._page.get(url)
            if result is not None:
                self._page = result
            return

        if self._browser is not None and hasattr(self._browser, "get"):
            result = await self._browser.get(url)
            if result is not None:
                self._page = result
            return

        await self._page.send(uc.cdp.page.navigate(url=url))

    async def _eval_js(self, script: str) -> Any:
        """Evaluate JavaScript with small compatibility fallbacks."""
        if not self._page:
            return None
        try:
            return await self._page.evaluate(script)
        except Exception:
            if "document.readyState" in script:
                return await self._page.evaluate("document.readyState")
            raise

    async def navigate(
        self,
        url:            str,
        wait_condition: str    = "domcontentloaded",
        timeout:        float  = 30.0,
        retry_count:    int    = 2,
    ) -> bool:
        """Navigate to URL and verify document readiness.

        nodriver's ``wait_for`` is selector-oriented in several versions. The
        previous code passed a JavaScript readyState expression to it, which can
        fail immediately or wait for a selector that will never exist. For the
        QA/reporting flow we only need controlled page-load verification, so we
        navigate and poll ``document.readyState`` directly.
        """
        if not self._page:
            raise PageLoadError(url=url, status_code=0)
        self._state    = BrowserState.NAVIGATING
        self._last_used = time.monotonic()
        for attempt in range(retry_count + 1):
            try:
                start = time.monotonic()
                await self._navigate_with_nodriver(url)

                deadline = time.monotonic() + max(1.0, timeout)
                while time.monotonic() < deadline:
                    try:
                        ready_state = await self._eval_js(
                            "(() => document.readyState)()"
                        )
                    except Exception:
                        ready_state = ""
                    if wait_condition == "load":
                        if ready_state == "complete":
                            break
                    elif wait_condition == "networkidle":
                        if ready_state in ("interactive", "complete"):
                            await asyncio.sleep(random.uniform(2.0, 3.0))
                            break
                    else:
                        if ready_state in ("interactive", "complete"):
                            break
                    await asyncio.sleep(0.25)
                else:
                    raise asyncio.TimeoutError()

                load_time = (time.monotonic() - start) * 1000
                self._pages_loaded += 1
                self._last_verified_url = url
                observed_url = await self.get_current_url()
                if self._is_internal_error_url(observed_url):
                    self._last_final_url = observed_url
                    self._last_url_source = self._last_url_source or "browser_internal_url"
                    self._state = BrowserState.READY
                    logger.warning(
                        f"[BrowserInstance] Browser reached internal error page: "
                        f"target={url[:80]} final={observed_url[:80]}"
                    )
                    return False

                if observed_url and not self._is_web_url(observed_url):
                    self._last_final_url = observed_url
                    self._last_url_source = self._last_url_source or "non_web_final_url"
                    self._state = BrowserState.READY
                    logger.warning(
                        f"[BrowserInstance] Non-web final URL after navigation: "
                        f"target={url[:80]} final={observed_url[:80]}"
                    )
                    return False

                self._last_final_url = observed_url or url
                if not self._last_url_source:
                    self._last_url_source = "last_verified_url"
                self._state = BrowserState.READY
                logger.info(
                    f"[BrowserInstance] Page load verified: "
                    f"{url[:80]} | load={load_time:.0f}ms"
                )
                return True
            except asyncio.TimeoutError:
                if attempt < retry_count:
                    await asyncio.sleep(2.0)
                else:
                    self._errors += 1
                    self._state = BrowserState.READY
                    logger.warning(f"[BrowserInstance] Page load timeout: {url[:80]}")
                    return False
            except Exception as exc:
                if attempt < retry_count:
                    await asyncio.sleep(1.0)
                else:
                    self._errors += 1
                    self._state = BrowserState.READY
                    logger.warning(f"[BrowserInstance] Navigation failed: {url[:80]} | {exc}")
                    return False
        return False

    async def execute_script(
        self, script: str, *args) -> Any:
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
        self, selector: str) -> List[Any]:
        if not self._page:
            return []
        try:
            elements = await self._page.select_all(selector)
            return elements or []
        except Exception:
            return []

    async def click_element(self, element: Any) -> bool:
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
        self._last_url_source = ""
        try:
            href = await self._eval_js(
                "(() => window.location && window.location.href ? window.location.href : '')()"
            )
            if href:
                self._last_final_url = href
                self._last_url_source = "window.location.href"
                return href
        except Exception:
            pass
        try:
            page_url = getattr(self._page, "url", "") or ""
            if page_url:
                self._last_final_url = page_url
                self._last_url_source = "tab.url"
                return page_url
        except Exception:
            pass
        if self._last_final_url:
            self._last_url_source = "last_final_url"
            return self._last_final_url
        if self._last_verified_url:
            self._last_url_source = "last_verified_url"
            return self._last_verified_url
        return ""

    def get_url_source(self) -> str:
        """Return how the latest URL was obtained for reporting/debugging."""
        try:
            return self._last_url_source or ""
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
            title = await self._eval_js(
                "(() => document.title)()"
            )
            return title or ""
        except Exception:
            return ""

    async def take_screenshot(
        self, path: Optional[str] = None) -> Optional[bytes]:
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
        self, cookie: Dict[str, Any]) -> None:
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
        self._state = BrowserState.DESTROYED
        try:
            if self._browser:
                try:
                    self._browser.stop()
                except Exception:
                    pass
                self._browser = None
                self._page    = None
        except Exception as exc:
            logger.debug(
                f"[BrowserInstance] Destroy error: {exc}"
            )
        finally:
            # Hard cleanup: nodriver/Chrome can occasionally leave a process or
            # isolated profile directory behind after .stop(). Kill the process
            # tree and remove the temporary profile so the app can close cleanly.
            if HAS_PSUTIL and self._pid:
                try:
                    proc = psutil.Process(self._pid)
                    for child in proc.children(recursive=True):
                        try:
                            child.kill()
                        except Exception:
                            pass
                    if proc.is_running():
                        proc.kill()
                except Exception:
                    pass
            if HAS_PSUTIL:
                self._kill_owned_chrome_processes(
                    profile_dir=self._profile_dir,
                    marker="jtp_isolated_",
                )
            if self._profile_dir:
                try:
                    shutil.rmtree(self._profile_dir, ignore_errors=True)
                except Exception:
                    pass
                self._profile_dir = None
            self._pid = None
            logger.debug(
                f"[BrowserInstance] Destroyed: "
                f"{self.browser_id}"
            )

    @staticmethod
    def _process_cmdline(proc: Any) -> str:
        try:
            return " ".join(proc.cmdline() or [])
        except Exception:
            return ""

    @classmethod
    def _kill_owned_chrome_processes(
        cls,
        profile_dir: Optional[str] = None,
        marker: str = "jtp_isolated_",
    ) -> int:
        """Kill only Chrome processes created with this app's temp profile marker."""
        if not HAS_PSUTIL:
            return 0

        killed = 0
        profile_dir_norm = str(profile_dir or "").replace("\\", "/")

        for proc in psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                name = (proc.info.get("name") or "").lower()
                if "chrome" not in name and "chromium" not in name:
                    continue

                cmdline = " ".join(proc.info.get("cmdline") or [])
                cmdline_norm = cmdline.replace("\\", "/")

                owns_profile = False
                if profile_dir_norm and profile_dir_norm in cmdline_norm:
                    owns_profile = True
                elif marker and marker in cmdline_norm:
                    owns_profile = True

                if not owns_profile:
                    continue

                children = []
                try:
                    children = proc.children(recursive=True)
                except Exception:
                    children = []

                for child in children:
                    try:
                        child.kill()
                        killed += 1
                    except Exception:
                        pass

                try:
                    proc.kill()
                    killed += 1
                except Exception:
                    pass
            except Exception:
                continue

        return killed

    @classmethod
    def cleanup_orphaned_chrome_processes(cls, marker: str = "jtp_isolated_") -> int:
        """Cleanup leftover app-owned Chrome processes from earlier failed runs."""
        killed = cls._kill_owned_chrome_processes(marker=marker)
        if killed:
            logger.warning(
                f"[BrowserInstance] Cleaned up {killed} orphaned app Chrome processes"
            )
        return killed

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
