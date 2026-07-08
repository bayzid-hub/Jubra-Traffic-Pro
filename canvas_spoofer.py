"""
Jubra Traffic Pro - Canvas Fingerprint Spoofer
Advanced canvas noise injection with per-session seeding,
WebGL spoofing, and fingerprint consistency management.
"""

import random
import hashlib
import logging
import asyncio
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class CanvasProfile:
    """Canvas fingerprint profile for a session."""
    noise_seed:         int
    noise_intensity:    float
    pixel_bias_r:       int
    pixel_bias_g:       int
    pixel_bias_b:       int
    text_metric_bias:   float
    arc_bias:           float
    session_id:         str     = ""

    @classmethod
    def generate(cls, session_id: str = "") -> "CanvasProfile":
        """Generate a unique canvas profile."""
        seed = random.randint(1, 2**31 - 1)
        rng  = random.Random(seed)
        return cls(
            noise_seed      = seed,
            noise_intensity = rng.uniform(0.001, 0.008),
            pixel_bias_r    = rng.randint(-2, 2),
            pixel_bias_g    = rng.randint(-2, 2),
            pixel_bias_b    = rng.randint(-2, 2),
            text_metric_bias = rng.uniform(-0.02, 0.02),
            arc_bias        = rng.uniform(-0.001, 0.001),
            session_id      = session_id,
        )

    def get_fingerprint_hash(self) -> str:
        raw = f"{self.noise_seed}:{self.pixel_bias_r}:{self.pixel_bias_g}"
        return hashlib.md5(raw.encode()).hexdigest()[:12]


class CanvasSpoofer:
    """
    Canvas Fingerprint Spoofer.

    Injects noise into:
    ─────────────────────────────────────────────────────
    • Canvas 2D toDataURL()
    • Canvas 2D toBlob()
    • getImageData()
    • measureText() - subtle width bias
    • arc() - sub-pixel bias
    • WebGL readPixels()
    • WebGL getParameter()
    """

    def __init__(self):
        self._profiles: Dict[str, CanvasProfile] = {}

    def get_profile(self, session_id: str) -> CanvasProfile:
        """Get or create canvas profile for session."""
        if session_id not in self._profiles:
            self._profiles[session_id] = CanvasProfile.generate(session_id)
        return self._profiles[session_id]

    def release_session(self, session_id: str) -> None:
        self._profiles.pop(session_id, None)

    def generate_injection_script(
        self,
        profile: CanvasProfile,
    ) -> str:
        """Generate complete canvas spoofing script."""
        return f"""
        (function() {{
            const SEED = {profile.noise_seed};
            const INTENSITY = {profile.noise_intensity:.6f};
            const BIAS_R = {profile.pixel_bias_r};
            const BIAS_G = {profile.pixel_bias_g};
            const BIAS_B = {profile.pixel_bias_b};
            const TEXT_BIAS = {profile.text_metric_bias:.4f};

            // Seeded PRNG (Mulberry32)
            function prng(seed) {{
                return function() {{
                    seed |= 0;
                    seed = seed + 0x6D2B79F5 | 0;
                    let t = Math.imul(seed ^ seed >>> 15, 1 | seed);
                    t = t + Math.imul(t ^ t >>> 7, 61 | t) ^ t;
                    return ((t ^ t >>> 14) >>> 0) / 4294967296;
                }};
            }}

            const rng = prng(SEED);

            // ── Canvas 2D Spoofing ───────────────────────
            const origToDataURL    = HTMLCanvasElement.prototype.toDataURL;
            const origToBlob       = HTMLCanvasElement.prototype.toBlob;
            const origGetImageData = CanvasRenderingContext2D.prototype.getImageData;
            const origMeasureText  = CanvasRenderingContext2D.prototype.measureText;
            const origArc          = CanvasRenderingContext2D.prototype.arc;

            function addPixelNoise(canvas) {{
                try {{
                    const ctx = canvas.getContext('2d');
                    if (!ctx) return;
                    const w = canvas.width;
                    const h = canvas.height;
                    if (w === 0 || h === 0) return;

                    const imgData = origGetImageData.call(ctx, 0, 0, w, h);
                    const data    = imgData.data;
                    const localRng = prng(SEED + w * h);

                    for (let i = 0; i < data.length; i += 4) {{
                        const n = (localRng() - 0.5) * INTENSITY * 255;
                        data[i]     = Math.max(0, Math.min(255, data[i]     + BIAS_R + n));
                        data[i + 1] = Math.max(0, Math.min(255, data[i + 1] + BIAS_G + n));
                        data[i + 2] = Math.max(0, Math.min(255, data[i + 2] + BIAS_B + n));
                    }}
                    ctx.putImageData(imgData, 0, 0);
                }} catch(e) {{}}
            }}

            HTMLCanvasElement.prototype.toDataURL = function(...args) {{
                addPixelNoise(this);
                return origToDataURL.apply(this, args);
            }};

            HTMLCanvasElement.prototype.toBlob = function(cb, ...args) {{
                addPixelNoise(this);
                return origToBlob.call(this, cb, ...args);
            }};

            CanvasRenderingContext2D.prototype.getImageData = function(...args) {{
                const data = origGetImageData.apply(this, args);
                const localRng = prng(SEED + args[0] + args[1] * 997);
                for (let i = 0; i < data.data.length; i += 4) {{
                    const n = Math.round((localRng() - 0.5) * 3);
                    data.data[i]     = Math.max(0, Math.min(255, data.data[i]     + n));
                    data.data[i + 1] = Math.max(0, Math.min(255, data.data[i + 1] + n));
                    data.data[i + 2] = Math.max(0, Math.min(255, data.data[i + 2] + n));
                }}
                return data;
            }};

            // Subtle text measurement bias
            CanvasRenderingContext2D.prototype.measureText = function(text) {{
                const metrics = origMeasureText.call(this, text);
                const bias    = TEXT_BIAS * text.length * 0.01;
                return new Proxy(metrics, {{
                    get(target, prop) {{
                        if (prop === 'width') return target.width + bias;
                        const val = target[prop];
                        return typeof val === 'function'
                            ? val.bind(target) : val;
                    }}
                }});
            }};

            // Subtle arc position bias
            CanvasRenderingContext2D.prototype.arc = function(
                x, y, r, startAngle, endAngle, anticlockwise
            ) {{
                return origArc.call(
                    this,
                    x + {profile.arc_bias:.4f},
                    y + {profile.arc_bias:.4f},
                    r,
                    startAngle,
                    endAngle,
                    anticlockwise,
                );
            }};

            // ── WebGL Spoofing ───────────────────────────
            function spoofWebGL(ctx) {{
                if (!ctx) return;
                const origReadPixels = ctx.readPixels?.bind(ctx);
                if (!origReadPixels) return;

                ctx.readPixels = function(...args) {{
                    origReadPixels(...args);
                    const pixels = args[6];
                    if (pixels instanceof Uint8Array) {{
                        const localRng = prng(SEED + pixels.length);
                        for (let i = 0; i < pixels.length; i += 4) {{
                            const n = Math.round((localRng() - 0.5) * 2);
                            pixels[i]     = Math.max(0, Math.min(255, pixels[i]     + n));
                            pixels[i + 1] = Math.max(0, Math.min(255, pixels[i + 1] + n));
                            pixels[i + 2] = Math.max(0, Math.min(255, pixels[i + 2] + n));
                        }}
                    }}
                }};
            }}

            const origGetContext = HTMLCanvasElement.prototype.getContext;
            HTMLCanvasElement.prototype.getContext = function(type, ...args) {{
                const ctx = origGetContext.call(this, type, ...args);
                if (type === 'webgl' || type === 'webgl2') {{
                    spoofWebGL(ctx);
                }}
                return ctx;
            }};
        }})();
        """

    async def apply_to_driver(
        self,
        driver:     Any,
        session_id: str,
    ) -> bool:
        """Apply canvas spoofing to a browser driver."""
        profile = self.get_profile(session_id)
        script  = self.generate_injection_script(profile)
        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None,
                lambda: driver.execute_cdp_cmd(
                    "Page.addScriptToEvaluateOnNewDocument",
                    {"source": script},
                ),
            )
            await loop.run_in_executor(
                None,
                lambda: driver.execute_script(script),
            )
            logger.debug(
                f"[CanvasSpoofer] Applied to session: {session_id[:8]}"
            )
            return True
        except Exception as exc:
            logger.error(f"[CanvasSpoofer] Apply error: {exc}")
            return False