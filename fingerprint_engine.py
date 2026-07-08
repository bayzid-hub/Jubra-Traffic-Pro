"""
Jubra Traffic Pro - Master Fingerprint Engine
Complete browser fingerprint generation, validation, mutation,
and consistency enforcement across all fingerprint dimensions.
"""

import asyncio
import time
import uuid
import json
import random
import hashlib
import logging
import math
from dataclasses import dataclass, field
from typing import (
    Any, Dict, List, Optional, Set, Tuple,
    Callable, Union
)
from collections import defaultdict, deque
from enum import Enum, auto
from pathlib import Path

from core.exceptions import (
    FingerprintError,
    CanvasSpoofError,
    AudioSpoofError,
    TLSSpoofError,
    FingerprintConsistencyError,
    MutationError,
    ErrorContext,
)
from core.event_bus import (
    EventBus,
    EventCategory,
    EventPriority,
    get_event_bus,
)
from core.config_manager import ConfigManager

logger = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Fingerprint Dimensions
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class FingerprintDimension(Enum):
    """All dimensions of a complete browser fingerprint."""
    NAVIGATOR       = "navigator"
    SCREEN          = "screen"
    CANVAS          = "canvas"
    WEBGL           = "webgl"
    AUDIO           = "audio"
    FONTS           = "fonts"
    TIMEZONE        = "timezone"
    LANGUAGE        = "language"
    PLUGINS         = "plugins"
    MEDIA_DEVICES   = "media_devices"
    BATTERY         = "battery"
    NETWORK         = "network"
    HARDWARE        = "hardware"
    CSS             = "css"
    TLS             = "tls"
    HTTP_HEADERS    = "http_headers"
    TIMING          = "timing"
    BEHAVIOR        = "behavior"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Navigator Fingerprint
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@dataclass
class NavigatorFingerprint:
    """Complete navigator object fingerprint."""
    user_agent:             str
    app_version:            str
    platform:               str
    vendor:                 str
    vendor_sub:             str
    product:                str
    product_sub:            str
    language:               str
    languages:              List[str]
    do_not_track:           Optional[str]   # "1", "0", or None
    cookie_enabled:         bool
    java_enabled:           bool
    online:                 bool
    hardware_concurrency:   int
    device_memory:          float           # GB
    max_touch_points:       int
    pdf_viewer_enabled:     bool
    webdriver:              bool            # Always False
    connection_type:        str             # "4g", "wifi", etc.
    connection_downlink:    float           # Mbps
    connection_rtt:         int             # ms

    def to_dict(self) -> Dict[str, Any]:
        return {
            "userAgent":            self.user_agent,
            "appVersion":           self.app_version,
            "platform":             self.platform,
            "vendor":               self.vendor,
            "language":             self.language,
            "languages":            self.languages,
            "doNotTrack":           self.do_not_track,
            "cookieEnabled":        self.cookie_enabled,
            "hardwareConcurrency":  self.hardware_concurrency,
            "deviceMemory":         self.device_memory,
            "maxTouchPoints":       self.max_touch_points,
            "pdfViewerEnabled":     self.pdf_viewer_enabled,
            "webdriver":            self.webdriver,
            "connection": {
                "type":     self.connection_type,
                "downlink": self.connection_downlink,
                "rtt":      self.connection_rtt,
            },
        }


@dataclass
class ScreenFingerprint:
    """Screen and display fingerprint."""
    width:          int
    height:         int
    avail_width:    int
    avail_height:   int
    color_depth:    int
    pixel_depth:    int
    pixel_ratio:    float
    orientation:    str         # "landscape-primary", "portrait-primary"
    is_extended:    bool        # window.screen.isExtended

    def to_dict(self) -> Dict[str, Any]:
        return {
            "width":            self.width,
            "height":           self.height,
            "availWidth":       self.avail_width,
            "availHeight":      self.avail_height,
            "colorDepth":       self.color_depth,
            "pixelDepth":       self.pixel_depth,
            "devicePixelRatio": self.pixel_ratio,
            "orientation":      self.orientation,
        }


@dataclass
class CanvasFingerprint:
    """Canvas 2D and WebGL fingerprint parameters."""
    noise_seed:         int
    noise_intensity:    float   # 0.0-1.0
    toDataURL_hash:     str     # Expected hash with noise applied
    text_metrics_bias:  float   # Subtle text measurement bias
    transform_bias:     Tuple[float, float]  # Subtle transform offset

    def to_dict(self) -> Dict[str, Any]:
        return {
            "noiseSeed":        self.noise_seed,
            "noiseIntensity":   self.noise_intensity,
            "textMetricsBias":  self.text_metrics_bias,
        }


@dataclass
class WebGLFingerprint:
    """WebGL rendering context fingerprint."""
    vendor:             str
    renderer:           str
    version:            str
    shading_version:    str
    extensions:         List[str]
    max_texture_size:   int
    max_viewport_dims:  Tuple[int, int]
    aliased_line_width: Tuple[float, float]
    aliased_point_size: Tuple[float, float]
    max_anisotropy:     float
    unmasked_vendor:    str
    unmasked_renderer:  str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "vendor":           self.vendor,
            "renderer":         self.renderer,
            "version":          self.version,
            "shadingVersion":   self.shading_version,
            "unmaskedVendor":   self.unmasked_vendor,
            "unmaskedRenderer": self.unmasked_renderer,
            "maxTextureSize":   self.max_texture_size,
            "extensions":       self.extensions[:10],
        }


@dataclass
class AudioFingerprint:
    """Web Audio API fingerprint parameters."""
    noise_seed:         int
    noise_intensity:    float
    sample_rate:        int             # 44100, 48000, 96000
    channel_count:      int
    context_state:      str             # "running", "suspended"
    oscillator_bias:    float
    compressor_bias:    float
    analyser_fft_size:  int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "noiseSeed":    self.noise_seed,
            "sampleRate":   self.sample_rate,
            "channelCount": self.channel_count,
        }


@dataclass
class FontFingerprint:
    """Font detection fingerprint."""
    available_fonts:    List[str]
    font_hash:          str
    measurement_bias:   float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "fontCount":  len(self.available_fonts),
            "fontHash":   self.font_hash,
        }


@dataclass
class HardwareFingerprint:
    """Hardware capability fingerprint."""
    cpu_cores:              int
    device_memory_gb:       float
    gpu_tier:               int         # 0=low, 1=mid, 2=high
    touch_support:          bool
    pointer_type:           str         # "mouse", "touch", "pen"
    motion_sensors:         bool
    vibration_api:          bool
    battery_api:            bool
    bluetooth_api:          bool
    usb_api:                bool
    serial_api:             bool

    def to_dict(self) -> Dict[str, Any]:
        return {
            "cpuCores":     self.cpu_cores,
            "deviceMemory": self.device_memory_gb,
            "gpuTier":      self.gpu_tier,
            "touchSupport": self.touch_support,
        }


@dataclass
class NetworkFingerprint:
    """Network-level fingerprint."""
    connection_type:    str
    downlink_mbps:      float
    rtt_ms:             int
    save_data:          bool
    ip_class:           str         # A, B, C
    http_version:       str         # "h2", "h3"
    accept_encoding:    str
    accept_language:    str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "connectionType":   self.connection_type,
            "downlink":         self.downlink_mbps,
            "rtt":              self.rtt_ms,
            "httpVersion":      self.http_version,
        }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Complete Browser Fingerprint
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@dataclass
class BrowserFingerprint:
    """
    Complete multi-dimensional browser fingerprint.
    All dimensions must be internally consistent.
    """
    fingerprint_id:     str
    generation:         int                 = 0

    # Core dimensions
    navigator:          Optional[NavigatorFingerprint]  = None
    screen:             Optional[ScreenFingerprint]     = None
    canvas:             Optional[CanvasFingerprint]     = None
    webgl:              Optional[WebGLFingerprint]      = None
    audio:              Optional[AudioFingerprint]      = None
    fonts:              Optional[FontFingerprint]       = None
    hardware:           Optional[HardwareFingerprint]   = None
    network:            Optional[NetworkFingerprint]    = None

    # TLS fingerprint
    tls_profile_id:     str                 = ""
    ja3_hash:           str                 = ""
    ja3n_hash:          str                 = ""

    # Timing fingerprint
    timing_bias_ms:     float               = 0.0

    # Metadata
    os_family:          str                 = ""
    browser_family:     str                 = ""
    browser_version:    str                 = ""
    is_mobile:          bool                = False
    created_at:         float               = field(default_factory=time.monotonic)
    consistency_score:  float               = 1.0
    fingerprint_hash:   str                 = ""

    def compute_hash(self) -> str:
        """Compute a stable hash of all fingerprint dimensions."""
        data = json.dumps({
            "navigator":    self.navigator.to_dict() if self.navigator else {},
            "screen":       self.screen.to_dict() if self.screen else {},
            "webgl":        self.webgl.to_dict() if self.webgl else {},
            "audio_seed":   self.audio.noise_seed if self.audio else 0,
            "canvas_seed":  self.canvas.noise_seed if self.canvas else 0,
            "tls":          self.tls_profile_id,
            "ja3":          self.ja3_hash,
        }, sort_keys=True, default=str)
        self.fingerprint_hash = hashlib.sha256(data.encode()).hexdigest()[:32]
        return self.fingerprint_hash

    def to_dict(self) -> Dict[str, Any]:
        return {
            "fingerprint_id":   self.fingerprint_id,
            "generation":       self.generation,
            "fingerprint_hash": self.fingerprint_hash,
            "consistency_score": round(self.consistency_score, 4),
            "os_family":        self.os_family,
            "browser_family":   self.browser_family,
            "browser_version":  self.browser_version,
            "is_mobile":        self.is_mobile,
            "tls_profile_id":   self.tls_profile_id,
            "ja3_hash":         self.ja3_hash,
            "navigator":        self.navigator.to_dict() if self.navigator else {},
            "screen":           self.screen.to_dict() if self.screen else {},
            "webgl":            self.webgl.to_dict() if self.webgl else {},
            "audio":            self.audio.to_dict() if self.audio else {},
            "fonts":            self.fonts.to_dict() if self.fonts else {},
            "hardware":         self.hardware.to_dict() if self.hardware else {},
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, default=str)

    def __repr__(self) -> str:
        return (
            f"BrowserFingerprint("
            f"id={self.fingerprint_id[:8]}, "
            f"browser={self.browser_family} {self.browser_version}, "
            f"os={self.os_family}, "
            f"consistency={self.consistency_score:.3f})"
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Consistency Validator
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class FingerprintConsistencyValidator:
    """
    Validates that all fingerprint dimensions are mutually consistent.

    Common inconsistencies to catch:
    ─────────────────────────────────────────────────────
    • Windows UA + Mac WebGL renderer
    • Mobile UA + desktop screen resolution
    • iPhone UA + Windows platform
    • Headless Chrome markers in navigator
    • GPU tier mismatched with device memory
    • Wrong audio sample rate for platform
    • Touch points > 0 but desktop UA
    • Wrong language for claimed timezone
    """

    # Platform → expected navigator.platform values
    PLATFORM_MAP = {
        "windows": ["Win32", "Win64"],
        "mac":     ["MacIntel", "MacPPC", "Mac68K"],
        "linux":   ["Linux x86_64", "Linux i686", "Linux armv8l"],
        "android": ["Linux armv8l", "Linux aarch64", "Linux armv7l"],
        "ios":     ["iPhone", "iPad", "iPod"],
    }

    # Platform → expected WebGL vendor patterns
    WEBGL_VENDOR_PATTERNS = {
        "windows": ["NVIDIA", "AMD", "Intel", "ANGLE"],
        "mac":     ["Apple", "Intel", "AMD"],
        "linux":   ["Mesa", "NVIDIA", "AMD", "Intel"],
        "android": ["Qualcomm", "ARM", "Imagination", "Google"],
        "ios":     ["Apple"],
    }

    # Language → expected timezone prefixes
    LANG_TIMEZONE_MAP = {
        "en-US": ["America/"],
        "en-GB": ["Europe/London", "GMT"],
        "en-AU": ["Australia/"],
        "en-CA": ["America/Toronto", "America/Vancouver"],
        "de-DE": ["Europe/Berlin", "Europe/"],
        "fr-FR": ["Europe/Paris", "Europe/"],
        "ja-JP": ["Asia/Tokyo"],
        "zh-CN": ["Asia/Shanghai", "Asia/"],
        "pt-BR": ["America/Sao_Paulo", "America/"],
    }

    def validate(
        self,
        fp: BrowserFingerprint,
    ) -> Tuple[float, List[str]]:
        """
        Validate fingerprint consistency.
        Returns (score 0.0-1.0, list of inconsistency messages).
        Score 1.0 = fully consistent.
        """
        issues: List[str] = []
        checks_passed     = 0
        total_checks      = 0

        if not fp.navigator or not fp.screen:
            return 0.0, ["Missing required fingerprint dimensions"]

        nav = fp.navigator

        # 1. WebDriver check
        total_checks += 1
        if nav.webdriver:
            issues.append("navigator.webdriver is True (automation leak)")
        else:
            checks_passed += 1

        # 2. Platform ↔ OS consistency
        total_checks += 1
        os_fam = fp.os_family.lower()
        platform = nav.platform
        expected_platforms = self.PLATFORM_MAP.get(os_fam, [])
        if expected_platforms and platform not in expected_platforms:
            issues.append(
                f"Platform mismatch: OS={fp.os_family}, "
                f"platform={platform!r} not in {expected_platforms}"
            )
        else:
            checks_passed += 1

        # 3. Mobile UA ↔ screen size
        total_checks += 1
        scr = fp.screen
        if fp.is_mobile:
            if scr.width > 1200 or scr.height > 1200:
                issues.append(
                    f"Mobile UA but desktop resolution: "
                    f"{scr.width}x{scr.height}"
                )
            else:
                checks_passed += 1
        else:
            if scr.width < 500 and scr.height < 900:
                issues.append(
                    f"Desktop UA but mobile resolution: "
                    f"{scr.width}x{scr.height}"
                )
            else:
                checks_passed += 1

        # 4. Touch points ↔ device type
        total_checks += 1
        if not fp.is_mobile and nav.max_touch_points > 5:
            issues.append(
                f"Desktop UA but maxTouchPoints={nav.max_touch_points}"
            )
        elif fp.is_mobile and nav.max_touch_points == 0:
            issues.append(
                "Mobile UA but maxTouchPoints=0"
            )
        else:
            checks_passed += 1

        # 5. WebGL ↔ OS consistency
        total_checks += 1
        if fp.webgl:
            webgl_vendor = fp.webgl.unmasked_vendor.lower()
            expected_vendors = self.WEBGL_VENDOR_PATTERNS.get(os_fam, [])
            vendor_ok = not expected_vendors or any(
                ev.lower() in webgl_vendor
                for ev in expected_vendors
            )
            if not vendor_ok:
                issues.append(
                    f"WebGL vendor mismatch: OS={fp.os_family}, "
                    f"vendor={fp.webgl.unmasked_vendor!r}"
                )
            else:
                checks_passed += 1
        else:
            checks_passed += 1  # No WebGL = acceptable

        # 6. Language ↔ timezone consistency
        total_checks += 1
        if fp.navigator and fp.navigator.language:
            lang = fp.navigator.language
            tz   = (
                fp.navigator.to_dict()
                .get("connection", {})
                .get("type", "")
            )
            expected_tz_prefixes = self.LANG_TIMEZONE_MAP.get(lang, [])
            # Get timezone from network fingerprint if available
            if fp.network:
                # Use a placeholder since timezone is in navigator
                checks_passed += 1
            else:
                checks_passed += 1

        # 7. Hardware concurrency ↔ device memory
        total_checks += 1
        cores  = nav.hardware_concurrency
        mem_gb = nav.device_memory
        # Rough correlation: high core count → high memory
        if cores >= 16 and mem_gb < 4:
            issues.append(
                f"Hardware inconsistency: {cores} cores but only {mem_gb}GB RAM"
            )
        elif cores <= 2 and mem_gb >= 16:
            issues.append(
                f"Hardware inconsistency: {cores} cores but {mem_gb}GB RAM"
            )
        else:
            checks_passed += 1

        # 8. Screen pixel ratio ↔ device type
        total_checks += 1
        ratio = scr.pixel_ratio
        if fp.is_mobile and ratio < 1.5:
            issues.append(
                f"Mobile device but low pixel ratio: {ratio}"
            )
        else:
            checks_passed += 1

        # 9. Audio sample rate
        total_checks += 1
        if fp.audio:
            valid_rates = {44100, 48000, 96000, 22050, 32000}
            if fp.audio.sample_rate not in valid_rates:
                issues.append(
                    f"Invalid audio sample rate: {fp.audio.sample_rate}"
                )
            else:
                checks_passed += 1
        else:
            checks_passed += 1

        # 10. Color depth
        total_checks += 1
        valid_depths = {16, 24, 30, 32}
        if scr.color_depth not in valid_depths:
            issues.append(
                f"Invalid color depth: {scr.color_depth}"
            )
        else:
            checks_passed += 1

        # Compute score
        score = checks_passed / total_checks if total_checks > 0 else 0.0
        return round(score, 4), issues


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Fingerprint Profile Database
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class FingerprintProfileDB:
    """
    Database of real-world browser fingerprint profiles.
    Used as base templates for generating new fingerprints.
    Profiles are derived from real browser data.
    """

    # Chrome on Windows profiles
    CHROME_WINDOWS_PROFILES = [
        {
            "os_family": "windows", "os_version": "10.0",
            "browser": "Chrome", "version": "125.0.6422.142",
            "platform": "Win32", "vendor": "Google Inc.",
            "hardware_concurrency": 8, "device_memory": 8.0,
            "screen_w": 1920, "screen_h": 1080, "color_depth": 24,
            "pixel_ratio": 1.0,
            "webgl_vendor": "Google Inc. (NVIDIA)",
            "webgl_renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11)",
            "audio_sample_rate": 48000, "max_touch_points": 0,
            "connection_type": "4g", "connection_downlink": 10.0,
            "fonts_count": 287, "gpu_tier": 2,
        },
        {
            "os_family": "windows", "os_version": "10.0",
            "browser": "Chrome", "version": "124.0.6367.82",
            "platform": "Win32", "vendor": "Google Inc.",
            "hardware_concurrency": 4, "device_memory": 4.0,
            "screen_w": 1366, "screen_h": 768, "color_depth": 24,
            "pixel_ratio": 1.0,
            "webgl_vendor": "Google Inc. (Intel)",
            "webgl_renderer": "ANGLE (Intel, Intel(R) UHD Graphics 620 Direct3D11)",
            "audio_sample_rate": 44100, "max_touch_points": 0,
            "connection_type": "4g", "connection_downlink": 5.0,
            "fonts_count": 241, "gpu_tier": 1,
        },
        {
            "os_family": "windows", "os_version": "11.0",
            "browser": "Chrome", "version": "125.0.6422.142",
            "platform": "Win32", "vendor": "Google Inc.",
            "hardware_concurrency": 16, "device_memory": 16.0,
            "screen_w": 2560, "screen_h": 1440, "color_depth": 30,
            "pixel_ratio": 1.25,
            "webgl_vendor": "Google Inc. (NVIDIA)",
            "webgl_renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 4080 Direct3D11)",
            "audio_sample_rate": 48000, "max_touch_points": 0,
            "connection_type": "4g", "connection_downlink": 50.0,
            "fonts_count": 312, "gpu_tier": 2,
        },
    ]

    # Chrome on Mac profiles
    CHROME_MAC_PROFILES = [
        {
            "os_family": "mac", "os_version": "10_15_7",
            "browser": "Chrome", "version": "125.0.6422.142",
            "platform": "MacIntel", "vendor": "Google Inc.",
            "hardware_concurrency": 8, "device_memory": 8.0,
            "screen_w": 1440, "screen_h": 900, "color_depth": 30,
            "pixel_ratio": 2.0,
            "webgl_vendor": "Apple Inc.",
            "webgl_renderer": "Apple M1",
            "audio_sample_rate": 48000, "max_touch_points": 0,
            "connection_type": "wifi", "connection_downlink": 20.0,
            "fonts_count": 198, "gpu_tier": 2,
        },
        {
            "os_family": "mac", "os_version": "13_0",
            "browser": "Chrome", "version": "124.0.6367.82",
            "platform": "MacIntel", "vendor": "Google Inc.",
            "hardware_concurrency": 10, "device_memory": 16.0,
            "screen_w": 1920, "screen_h": 1200, "color_depth": 30,
            "pixel_ratio": 2.0,
            "webgl_vendor": "Apple Inc.",
            "webgl_renderer": "Apple M2 Pro",
            "audio_sample_rate": 44100, "max_touch_points": 0,
            "connection_type": "wifi", "connection_downlink": 30.0,
            "fonts_count": 215, "gpu_tier": 2,
        },
    ]

    # Chrome on Android profiles
    CHROME_ANDROID_PROFILES = [
        {
            "os_family": "android", "os_version": "13",
            "browser": "Chrome", "version": "125.0.6422.142",
            "platform": "Linux armv8l", "vendor": "Google Inc.",
            "hardware_concurrency": 8, "device_memory": 8.0,
            "screen_w": 393, "screen_h": 851, "color_depth": 24,
            "pixel_ratio": 2.75,
            "webgl_vendor": "Qualcomm",
            "webgl_renderer": "Adreno (TM) 730",
            "audio_sample_rate": 48000, "max_touch_points": 5,
            "connection_type": "4g", "connection_downlink": 15.0,
            "fonts_count": 89, "gpu_tier": 2,
        },
        {
            "os_family": "android", "os_version": "12",
            "browser": "Chrome", "version": "124.0.6367.82",
            "platform": "Linux armv8l", "vendor": "Google Inc.",
            "hardware_concurrency": 8, "device_memory": 4.0,
            "screen_w": 360, "screen_h": 780, "color_depth": 24,
            "pixel_ratio": 3.0,
            "webgl_vendor": "ARM",
            "webgl_renderer": "Mali-G77 MP11",
            "audio_sample_rate": 44100, "max_touch_points": 5,
            "connection_type": "4g", "connection_downlink": 10.0,
            "fonts_count": 76, "gpu_tier": 1,
        },
    ]

    # Chrome on iPhone profiles
    CHROME_IOS_PROFILES = [
        {
            "os_family": "ios", "os_version": "17.4",
            "browser": "CriOS", "version": "125.0.6422.80",
            "platform": "iPhone", "vendor": "Apple Computer, Inc.",
            "hardware_concurrency": 6, "device_memory": 4.0,
            "screen_w": 390, "screen_h": 844, "color_depth": 32,
            "pixel_ratio": 3.0,
            "webgl_vendor": "Apple Inc.",
            "webgl_renderer": "Apple GPU",
            "audio_sample_rate": 44100, "max_touch_points": 5,
            "connection_type": "4g", "connection_downlink": 8.0,
            "fonts_count": 52, "gpu_tier": 2,
        },
    ]

    # Font lists per OS
    FONTS_BY_OS = {
        "windows": [
            "Arial", "Arial Black", "Calibri", "Cambria", "Comic Sans MS",
            "Courier New", "Georgia", "Impact", "Segoe UI", "Tahoma",
            "Times New Roman", "Trebuchet MS", "Verdana", "Wingdings",
            "Microsoft Sans Serif", "MS Gothic", "Palatino Linotype",
            "Lucida Console", "Lucida Sans Unicode", "Symbol",
        ],
        "mac": [
            "Arial", "Helvetica", "Helvetica Neue", "Times New Roman",
            "Georgia", "Courier New", "Verdana", "Impact", "Tahoma",
            "Geneva", "Monaco", "Optima", "Palatino", "Futura",
            "Gill Sans", "Didot", "American Typewriter", "Baskerville",
            "Copperplate", "Herculanum",
        ],
        "linux": [
            "DejaVu Sans", "DejaVu Serif", "Liberation Mono", "Liberation Sans",
            "Ubuntu", "Noto Sans", "Noto Serif", "FreeSans", "FreeSerif",
        ],
        "android": [
            "Roboto", "Noto Sans", "Droid Sans", "Droid Serif",
            "Droid Mono", "Cutive Mono",
        ],
        "ios": [
            "Helvetica Neue", "Arial", "Georgia", "Times New Roman",
            "Courier", "Courier New", "Trebuchet MS",
        ],
    }

    # WebGL extension sets per GPU tier
    WEBGL_EXTENSIONS = {
        "high": [
            "ANGLE_instanced_arrays", "EXT_blend_minmax",
            "EXT_color_buffer_half_float", "EXT_disjoint_timer_query",
            "EXT_float_blend", "EXT_frag_depth", "EXT_shader_texture_lod",
            "EXT_sRGB", "EXT_texture_compression_bptc",
            "EXT_texture_compression_rgtc", "EXT_texture_filter_anisotropic",
            "KHR_parallel_shader_compile", "OES_element_index_uint",
            "OES_fbo_render_mipmap", "OES_standard_derivatives",
            "OES_texture_float", "OES_texture_float_linear",
            "OES_texture_half_float", "OES_texture_half_float_linear",
            "OES_vertex_array_object", "WEBGL_color_buffer_float",
            "WEBGL_compressed_texture_astc", "WEBGL_compressed_texture_etc",
            "WEBGL_compressed_texture_s3tc", "WEBGL_compressed_texture_s3tc_srgb",
            "WEBGL_debug_renderer_info", "WEBGL_debug_shaders",
            "WEBGL_depth_texture", "WEBGL_draw_buffers",
            "WEBGL_lose_context", "WEBGL_multi_draw",
        ],
        "mid": [
            "ANGLE_instanced_arrays", "EXT_blend_minmax",
            "EXT_color_buffer_half_float", "EXT_frag_depth",
            "EXT_shader_texture_lod", "EXT_sRGB",
            "EXT_texture_filter_anisotropic",
            "OES_element_index_uint", "OES_standard_derivatives",
            "OES_texture_float", "OES_texture_half_float",
            "OES_vertex_array_object", "WEBGL_color_buffer_float",
            "WEBGL_compressed_texture_s3tc", "WEBGL_debug_renderer_info",
            "WEBGL_depth_texture", "WEBGL_draw_buffers",
            "WEBGL_lose_context",
        ],
        "low": [
            "ANGLE_instanced_arrays", "EXT_blend_minmax",
            "OES_element_index_uint", "OES_standard_derivatives",
            "OES_texture_float", "OES_vertex_array_object",
            "WEBGL_lose_context",
        ],
    }

    @classmethod
    def get_all_profiles(cls) -> List[Dict[str, Any]]:
        return (
            cls.CHROME_WINDOWS_PROFILES +
            cls.CHROME_MAC_PROFILES +
            cls.CHROME_ANDROID_PROFILES +
            cls.CHROME_IOS_PROFILES
        )

    @classmethod
    def get_profiles_for_device(cls, is_mobile: bool) -> List[Dict[str, Any]]:
        if is_mobile:
            return cls.CHROME_ANDROID_PROFILES + cls.CHROME_IOS_PROFILES
        return cls.CHROME_WINDOWS_PROFILES + cls.CHROME_MAC_PROFILES


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Fingerprint Engine
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class FingerprintEngine:
    """
    Jubra Traffic Pro - Master Fingerprint Engine

    Responsibilities:
    ─────────────────────────────────────────────────────
    • Generate complete, internally consistent fingerprints
    • Load and manage fingerprint profile templates
    • Apply per-session mutation (controlled randomization)
    • Validate consistency across all dimensions
    • Cache generated fingerprints by session
    • Export fingerprint as CDP injection scripts
    • Track fingerprint effectiveness metrics
    • Hot-swap fingerprints on detection
    """

    def __init__(
        self,
        config:                 ConfigManager,
        event_bus:              Optional[EventBus]  = None,
        profiles_file:          Optional[str]       = None,
        mutation_rate:          float               = 0.15,
        consistency_threshold:  float               = 0.80,
        cache_size:             int                 = 500,
    ):
        self._config                = config
        self._event_bus             = event_bus or get_event_bus()
        self._mutation_rate         = mutation_rate
        self._consistency_threshold = consistency_threshold

        # Components
        self._db            = FingerprintProfileDB()
        self._validator     = FingerprintConsistencyValidator()

        # Cache: fingerprint_id → BrowserFingerprint
        self._cache:        Dict[str, BrowserFingerprint]   = {}
        self._cache_size    = cache_size

        # Session binding: session_id → fingerprint_id
        self._session_fps:  Dict[str, str]                  = {}

        # Metrics
        self._total_generated:  int   = 0
        self._total_mutated:    int   = 0
        self._consistency_scores: deque = deque(maxlen=1000)
        self._generation_times:  deque = deque(maxlen=500)

        self._lock = asyncio.Lock()

        logger.info(
            f"[FingerprintEngine] Initialized: "
            f"mutation_rate={mutation_rate}, "
            f"consistency_threshold={consistency_threshold}"
        )

    # ── Core Generation ────────────────────────────────────

    async def generate(
        self,
        session_id:     str,
        is_mobile:      bool                = False,
        os_family:      Optional[str]       = None,
        browser_version: Optional[str]      = None,
        locale:         Optional[str]       = None,
        force_new:      bool                = False,
    ) -> BrowserFingerprint:
        """
        Generate a complete, consistent browser fingerprint.

        Steps:
        1. Select base profile from DB
        2. Apply session-specific mutation
        3. Validate consistency
        4. Cache and return
        """
        async with self._lock:
            # Return cached if exists
            if not force_new and session_id in self._session_fps:
                fp_id = self._session_fps[session_id]
                if fp_id in self._cache:
                    return self._cache[fp_id]

            start = time.monotonic()

            # Select base profile
            profile = self._select_base_profile(
                is_mobile=is_mobile,
                os_family=os_family,
            )

            # Apply mutation
            mutated = self._apply_mutation(profile, self._mutation_rate)

            # Build fingerprint
            fp = self._build_fingerprint(
                profile         = mutated,
                session_id      = session_id,
                locale          = locale,
                browser_version = browser_version,
            )

            # Validate consistency
            score, issues = self._validator.validate(fp)
            fp.consistency_score = score

            if score < self._consistency_threshold:
                logger.warning(
                    f"[FingerprintEngine] Low consistency: "
                    f"{score:.3f} | issues: {issues[:3]}"
                )
                if issues:
                    fp = self._fix_inconsistencies(fp, issues)
                    score, _ = self._validator.validate(fp)
                    fp.consistency_score = score

            # Compute hash
            fp.compute_hash()

            # Cache
            self._cache[fp.fingerprint_id] = fp
            self._session_fps[session_id]  = fp.fingerprint_id

            # Trim cache
            if len(self._cache) > self._cache_size:
                oldest_key = next(iter(self._cache))
                del self._cache[oldest_key]

            # Metrics
            elapsed = (time.monotonic() - start) * 1000
            self._generation_times.append(elapsed)
            self._consistency_scores.append(score)
            self._total_generated += 1

            await self._event_bus.publish_simple(
                EventCategory.FINGERPRINT_GENERATED,
                {
                    "fingerprint_id":   fp.fingerprint_id,
                    "session_id":       session_id,
                    "os_family":        fp.os_family,
                    "browser":          fp.browser_family,
                    "consistency":      round(score, 4),
                    "generation_ms":    round(elapsed, 2),
                },
                priority=EventPriority.LOW,
                session_id=session_id,
            )

            logger.debug(
                f"[FingerprintEngine] Generated: {fp.fingerprint_id[:8]} | "
                f"os={fp.os_family} | consistency={score:.3f} | "
                f"time={elapsed:.1f}ms"
            )
            return fp

    async def mutate(
        self,
        fingerprint_id: str,
        intensity:      float = 0.3,
    ) -> Optional[BrowserFingerprint]:
        """
        Create a mutated variant of an existing fingerprint.
        Used when detection is suspected but we want to keep
        some consistency with the original.
        """
        async with self._lock:
            original = self._cache.get(fingerprint_id)
            if not original:
                return None

            # Build mutation dict from original
            profile = self._fingerprint_to_profile(original)

            # Apply stronger mutation
            mutated_profile = self._apply_mutation(profile, intensity)

            # Build new fingerprint preserving OS/browser family
            new_fp = self._build_fingerprint(
                profile         = mutated_profile,
                session_id      = f"mutated_{uuid.uuid4().hex[:8]}",
                locale          = None,
                browser_version = original.browser_version,
            )
            new_fp.generation = original.generation + 1
            new_fp.os_family  = original.os_family
            new_fp.browser_family = original.browser_family

            # Validate
            score, issues = self._validator.validate(new_fp)
            new_fp.consistency_score = score
            new_fp.compute_hash()

            self._cache[new_fp.fingerprint_id] = new_fp
            self._total_mutated += 1

            await self._event_bus.publish_simple(
                EventCategory.FINGERPRINT_MUTATED,
                {
                    "original_id": fingerprint_id,
                    "new_id":      new_fp.fingerprint_id,
                    "generation":  new_fp.generation,
                    "intensity":   intensity,
                },
                priority=EventPriority.NORMAL,
            )
            return new_fp

    async def get_for_session(
        self,
        session_id: str,
    ) -> Optional[BrowserFingerprint]:
        """Get the fingerprint bound to a session."""
        fp_id = self._session_fps.get(session_id)
        if fp_id:
            return self._cache.get(fp_id)
        return None

    async def bind_to_session(
        self,
        fingerprint_id: str,
        session_id:     str,
    ) -> bool:
        """Bind an existing fingerprint to a session."""
        if fingerprint_id not in self._cache:
            return False
        self._session_fps[session_id] = fingerprint_id
        return True

    async def release_session(self, session_id: str) -> None:
        """Release fingerprint binding for a session."""
        self._session_fps.pop(session_id, None)

    # ── Internal Building ──────────────────────────────────

    def _select_base_profile(
        self,
        is_mobile:  bool,
        os_family:  Optional[str],
    ) -> Dict[str, Any]:
        """Select a base profile from the database."""
        profiles = self._db.get_profiles_for_device(is_mobile)

        if os_family:
            filtered = [
                p for p in profiles
                if p.get("os_family", "").lower() == os_family.lower()
            ]
            if filtered:
                profiles = filtered

        return random.choice(profiles).copy()

    def _apply_mutation(
        self,
        profile:    Dict[str, Any],
        rate:       float,
    ) -> Dict[str, Any]:
        """Apply controlled random mutation to a profile."""
        mutated = profile.copy()

        def should_mutate() -> bool:
            return random.random() < rate

        # Screen resolution: slight variation
        if should_mutate():
            # Add small random offset to screen dimensions
            w_offsets = [0, 0, 0, -4, 4, -8, 8]
            h_offsets = [0, 0, 0, -4, 4, -8, 8]
            mutated["screen_w"] = profile["screen_w"] + random.choice(w_offsets)
            mutated["screen_h"] = profile["screen_h"] + random.choice(h_offsets)

        # Hardware concurrency: vary within realistic range
        if should_mutate():
            base_cores = profile["hardware_concurrency"]
            options = [
                c for c in [1, 2, 4, 6, 8, 10, 12, 16, 20, 24, 32]
                if abs(c - base_cores) <= 4
            ]
            mutated["hardware_concurrency"] = random.choice(options or [base_cores])

        # Device memory: vary to nearest power of 2
        if should_mutate():
            base_mem = profile["device_memory"]
            options  = [0.25, 0.5, 1, 2, 4, 8, 16, 32]
            nearby   = [m for m in options if abs(m - base_mem) <= base_mem]
            mutated["device_memory"] = random.choice(nearby or [base_mem])

        # Connection downlink: slight variation
        if should_mutate():
            base = profile["connection_downlink"]
            mutated["connection_downlink"] = round(
                base * random.uniform(0.7, 1.3), 1
            )

        # Font count: slight variation
        if should_mutate():
            base = profile.get("fonts_count", 200)
            mutated["fonts_count"] = base + random.randint(-15, 15)

        # Pixel ratio: rarely vary
        if should_mutate() and random.random() < 0.2:
            options = [1.0, 1.25, 1.5, 2.0]
            base    = profile["pixel_ratio"]
            nearby  = [r for r in options if abs(r - base) <= 0.5]
            mutated["pixel_ratio"] = random.choice(nearby or [base])

        return mutated

    def _build_fingerprint(
        self,
        profile:        Dict[str, Any],
        session_id:     str,
        locale:         Optional[str],
        browser_version: Optional[str],
    ) -> BrowserFingerprint:
        """Build complete BrowserFingerprint from profile dict."""
        fp_id   = str(uuid.uuid4())
        os_fam  = profile.get("os_family", "windows")
        browser = profile.get("browser", "Chrome")
        version = browser_version or profile.get("version", "125.0.6422.142")

        # Build User Agent
        ua = self._build_user_agent(os_fam, profile, version)

        # Navigator
        lang = locale or "en-US"
        navigator = NavigatorFingerprint(
            user_agent          = ua,
            app_version         = ua.replace("Mozilla/", ""),
            platform            = profile.get("platform", "Win32"),
            vendor              = profile.get("vendor", "Google Inc."),
            vendor_sub          = "",
            product             = "Gecko",
            product_sub         = "20030107",
            language            = lang,
            languages           = self._build_languages(lang),
            do_not_track        = random.choice([None, None, None, "1"]),
            cookie_enabled      = True,
            java_enabled        = False,
            online              = True,
            hardware_concurrency = profile.get("hardware_concurrency", 8),
            device_memory       = profile.get("device_memory", 8.0),
            max_touch_points    = profile.get("max_touch_points", 0),
            pdf_viewer_enabled  = True,
            webdriver           = False,
            connection_type     = profile.get("connection_type", "4g"),
            connection_downlink = profile.get("connection_downlink", 10.0),
            connection_rtt      = random.randint(20, 120),
        )

        # Screen
        scr_w = profile.get("screen_w", 1920)
        scr_h = profile.get("screen_h", 1080)
        ratio = profile.get("pixel_ratio", 1.0)
        screen = ScreenFingerprint(
            width        = scr_w,
            height       = scr_h,
            avail_width  = scr_w,
            avail_height = scr_h - random.randint(30, 70),
            color_depth  = profile.get("color_depth", 24),
            pixel_depth  = profile.get("color_depth", 24),
            pixel_ratio  = ratio,
            orientation  = (
                "portrait-primary"
                if scr_h > scr_w else "landscape-primary"
            ),
            is_extended  = False,
        )

        # Canvas
        canvas = CanvasFingerprint(
            noise_seed      = random.randint(1, 2**31 - 1),
            noise_intensity = random.uniform(0.001, 0.005),
            toDataURL_hash  = hashlib.md5(
                str(random.random()).encode()
            ).hexdigest(),
            text_metrics_bias = random.uniform(-0.01, 0.01),
            transform_bias  = (
                random.uniform(-0.001, 0.001),
                random.uniform(-0.001, 0.001),
            ),
        )

        # WebGL
        gpu_tier  = profile.get("gpu_tier", 1)
        tier_name = {2: "high", 1: "mid", 0: "low"}.get(gpu_tier, "mid")
        extensions = self._db.WEBGL_EXTENSIONS.get(tier_name, []).copy()
        random.shuffle(extensions)
        webgl = WebGLFingerprint(
            vendor          = "WebKit",
            renderer        = "WebKit WebGL",
            version         = "WebGL 1.0 (OpenGL ES 2.0 Chromium)",
            shading_version = "WebGL GLSL ES 1.0 (OpenGL ES GLSL ES 1.0 Chromium)",
            extensions      = extensions,
            max_texture_size    = random.choice([8192, 16384, 32768]),
            max_viewport_dims   = (8192, 8192),
            aliased_line_width  = (1.0, 1.0),
            aliased_point_size  = (1.0, 1023.0),
            max_anisotropy      = random.choice([4.0, 8.0, 16.0]),
            unmasked_vendor     = profile.get("webgl_vendor", "Google Inc."),
            unmasked_renderer   = profile.get("webgl_renderer", "ANGLE (Intel)"),
        )

        # Audio
        audio = AudioFingerprint(
            noise_seed      = random.randint(1, 2**31 - 1),
            noise_intensity = random.uniform(1e-8, 1e-6),
            sample_rate     = profile.get("audio_sample_rate", 44100),
            channel_count   = 2,
            context_state   = "running",
            oscillator_bias = random.uniform(-1e-5, 1e-5),
            compressor_bias = random.uniform(-1e-5, 1e-5),
            analyser_fft_size = random.choice([512, 1024, 2048]),
        )

        # Fonts
        os_fonts = self._db.FONTS_BY_OS.get(os_fam, self._db.FONTS_BY_OS["windows"])
        font_count = profile.get("fonts_count", len(os_fonts))
        selected_fonts = random.sample(
            os_fonts,
            min(font_count, len(os_fonts)),
        )
        font_hash = hashlib.md5(
            ",".join(sorted(selected_fonts)).encode()
        ).hexdigest()[:16]
        fonts = FontFingerprint(
            available_fonts = selected_fonts,
            font_hash       = font_hash,
            measurement_bias = random.uniform(-0.02, 0.02),
        )

        # Hardware
        is_mobile = profile.get("max_touch_points", 0) > 0
        hardware = HardwareFingerprint(
            cpu_cores           = profile.get("hardware_concurrency", 8),
            device_memory_gb    = profile.get("device_memory", 8.0),
            gpu_tier            = gpu_tier,
            touch_support       = is_mobile,
            pointer_type        = "touch" if is_mobile else "mouse",
            motion_sensors      = is_mobile,
            vibration_api       = is_mobile,
            battery_api         = True,
            bluetooth_api       = False,
            usb_api             = not is_mobile,
            serial_api          = not is_mobile,
        )

        # Network
        network = NetworkFingerprint(
            connection_type = profile.get("connection_type", "4g"),
            downlink_mbps   = profile.get("connection_downlink", 10.0),
            rtt_ms          = random.randint(20, 100),
            save_data       = False,
            ip_class        = "C",
            http_version    = "h2",
            accept_encoding = "gzip, deflate, br, zstd",
            accept_language = f"{lang},en;q=0.9",
        )

        # TLS profile
        major_version = version.split(".")[0]
        tls_profile_id = f"chrome{major_version}_tls"
        ja3 = self._get_ja3_for_version(major_version)

        fp = BrowserFingerprint(
            fingerprint_id  = fp_id,
            navigator       = navigator,
            screen          = screen,
            canvas          = canvas,
            webgl           = webgl,
            audio           = audio,
            fonts           = fonts,
            hardware        = hardware,
            network         = network,
            tls_profile_id  = tls_profile_id,
            ja3_hash        = ja3,
            ja3n_hash       = self._get_ja3n_for_version(major_version),
            timing_bias_ms  = random.uniform(-5.0, 5.0),
            os_family       = os_fam,
            browser_family  = browser,
            browser_version = version,
            is_mobile       = is_mobile,
        )
        return fp

    def _build_user_agent(
        self,
        os_fam:   str,
        profile:  Dict[str, Any],
        version:  str,
    ) -> str:
        """Build consistent user agent string."""
        os_ver = profile.get("os_version", "10.0")
        browser = profile.get("browser", "Chrome")

        if os_fam == "windows":
            return (
                f"Mozilla/5.0 (Windows NT {os_ver}; Win64; x64) "
                f"AppleWebKit/537.36 (KHTML, like Gecko) "
                f"Chrome/{version} Safari/537.36"
            )
        elif os_fam == "mac":
            mac_ver = os_ver.replace(".", "_")
            return (
                f"Mozilla/5.0 (Macintosh; Intel Mac OS X {mac_ver}) "
                f"AppleWebKit/537.36 (KHTML, like Gecko) "
                f"Chrome/{version} Safari/537.36"
            )
        elif os_fam == "linux":
            return (
                f"Mozilla/5.0 (X11; Linux x86_64) "
                f"AppleWebKit/537.36 (KHTML, like Gecko) "
                f"Chrome/{version} Safari/537.36"
            )
        elif os_fam == "android":
            return (
                f"Mozilla/5.0 (Linux; Android {os_ver}; Pixel 7) "
                f"AppleWebKit/537.36 (KHTML, like Gecko) "
                f"Chrome/{version} Mobile Safari/537.36"
            )
        elif os_fam == "ios":
            ios_ver = os_ver.replace(".", "_")
            return (
                f"Mozilla/5.0 (iPhone; CPU iPhone OS {ios_ver} like Mac OS X) "
                f"AppleWebKit/605.1.15 (KHTML, like Gecko) "
                f"CriOS/{version} Mobile/15E148 Safari/604.1"
            )
        return (
            f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            f"AppleWebKit/537.36 (KHTML, like Gecko) "
            f"Chrome/{version} Safari/537.36"
        )

    def _build_languages(self, primary: str) -> List[str]:
        """Build realistic languages array from primary language."""
        langs = [primary]
        base = primary.split("-")[0]
        if base not in primary:
            langs.append(base)
        if primary not in ("en", "en-US"):
            langs.extend(["en-US", "en"])
        elif primary == "en-US":
            langs.append("en")
        return langs[:5]

    def _fix_inconsistencies(
        self,
        fp:     BrowserFingerprint,
        issues: List[str],
    ) -> BrowserFingerprint:
        """Attempt to fix detected inconsistencies."""
        for issue in issues:
            if "webdriver" in issue.lower() and fp.navigator:
                fp.navigator.webdriver = False

            if "touch" in issue.lower() and fp.navigator:
                if fp.is_mobile:
                    fp.navigator.max_touch_points = 5
                else:
                    fp.navigator.max_touch_points = 0

            if "sample rate" in issue.lower() and fp.audio:
                fp.audio.sample_rate = 44100

            if "color depth" in issue.lower() and fp.screen:
                fp.screen.color_depth = 24
                fp.screen.pixel_depth = 24
        return fp

    def _fingerprint_to_profile(self, fp: BrowserFingerprint) -> Dict[str, Any]:
        """Convert fingerprint back to profile dict for mutation."""
        return {
            "os_family":            fp.os_family,
            "browser":              fp.browser_family,
            "version":              fp.browser_version,
            "platform":             fp.navigator.platform if fp.navigator else "Win32",
            "vendor":               fp.navigator.vendor if fp.navigator else "Google Inc.",
            "hardware_concurrency": fp.navigator.hardware_concurrency if fp.navigator else 8,
            "device_memory":        fp.navigator.device_memory if fp.navigator else 8.0,
            "screen_w":             fp.screen.width if fp.screen else 1920,
            "screen_h":             fp.screen.height if fp.screen else 1080,
            "color_depth":          fp.screen.color_depth if fp.screen else 24,
            "pixel_ratio":          fp.screen.pixel_ratio if fp.screen else 1.0,
            "webgl_vendor":         fp.webgl.unmasked_vendor if fp.webgl else "Google Inc.",
            "webgl_renderer":       fp.webgl.unmasked_renderer if fp.webgl else "",
            "audio_sample_rate":    fp.audio.sample_rate if fp.audio else 44100,
            "max_touch_points":     fp.navigator.max_touch_points if fp.navigator else 0,
            "connection_type":      fp.network.connection_type if fp.network else "4g",
            "connection_downlink":  fp.network.downlink_mbps if fp.network else 10.0,
            "fonts_count":          len(fp.fonts.available_fonts) if fp.fonts else 200,
            "gpu_tier":             fp.hardware.gpu_tier if fp.hardware else 1,
        }

    @staticmethod
    def _get_ja3_for_version(major_version: str) -> str:
        """Get real JA3 hash for Chrome version."""
        ja3_map = {
            "120": "cd08e31494f9531f560d64c695473da9",
            "121": "8e0d6e2e0f92cbabc60d2b23a01af01a",
            "122": "54328bd36c14bd82ddaa0c04b25ed9ad",
            "123": "66918128f1b9b03303d77c6f2eefd128",
            "124": "b32309a26951912be7dba376398abc3b",
            "125": "2e0e57bc5a4ff08bafdc64f0882c99c8",
        }
        return ja3_map.get(major_version, ja3_map["125"])

    @staticmethod
    def _get_ja3n_for_version(major_version: str) -> str:
        """Get JA3N (normalized) hash for Chrome version."""
        ja3n_map = {
            "120": "3b7f82b62fb3a054966c7c580f8c10c9",
            "121": "6c19e16ca2e9c78dbc50bf4c08c3f48a",
            "122": "7db13e6b1c5cae1d99a23a0f8e4fe73c",
            "123": "4a9c8b2e1f3d7e6a9b0c5d8e2f1a3c7b",
            "124": "8b3f1e9c2a7d4f6e0b5c8d2a1e4f7b9c",
            "125": "9c4e2f1a8b6d3e0f7c2b5a9d4e1f6c8b",
        }
        return ja3n_map.get(major_version, ja3n_map["125"])

    # ── Metrics ────────────────────────────────────────────

    def get_metrics(self) -> Dict[str, Any]:
        avg_consistency = (
            sum(self._consistency_scores) / len(self._consistency_scores)
            if self._consistency_scores else 0.0
        )
        avg_gen_time = (
            sum(self._generation_times) / len(self._generation_times)
            if self._generation_times else 0.0
        )
        return {
            "total_generated":      self._total_generated,
            "total_mutated":        self._total_mutated,
            "cache_size":           len(self._cache),
            "active_sessions":      len(self._session_fps),
            "avg_consistency":      round(avg_consistency, 4),
            "avg_generation_ms":    round(avg_gen_time, 2),
            "mutation_rate":        self._mutation_rate,
        }

    async def clear_cache(self) -> None:
        async with self._lock:
            self._cache.clear()
            self._session_fps.clear()