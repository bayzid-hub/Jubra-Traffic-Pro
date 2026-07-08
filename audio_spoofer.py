"""
Jubra Traffic Pro - Audio Fingerprint Spoofer
AudioContext API spoofing with seeded noise injection
for AnalyserNode, OscillatorNode, and DynamicsCompressorNode.
"""

import random
import hashlib
import logging
import asyncio
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


@dataclass
class AudioProfile:
    """Audio fingerprint profile for a session."""
    noise_seed:         int
    noise_intensity:    float
    sample_rate:        int
    oscillator_bias:    float
    compressor_bias:    float
    analyser_fft_size:  int
    channel_count:      int
    session_id:         str = ""

    @classmethod
    def generate(cls, session_id: str = "") -> "AudioProfile":
        """Generate a unique audio profile."""
        seed = random.randint(1, 2**31 - 1)
        rng  = random.Random(seed)
        return cls(
            noise_seed      = seed,
            noise_intensity = rng.uniform(1e-8, 1e-6),
            sample_rate     = rng.choice([44100, 48000, 48000, 48000]),
            oscillator_bias = rng.uniform(-1e-5, 1e-5),
            compressor_bias = rng.uniform(-1e-5, 1e-5),
            analyser_fft_size = rng.choice([512, 1024, 2048]),
            channel_count   = 2,
            session_id      = session_id,
        )


class AudioSpoofer:
    """
    Web Audio API Fingerprint Spoofer.

    Patches:
    ─────────────────────────────────────────────────────
    • AudioContext.sampleRate
    • AudioBuffer.getChannelData()
    • AnalyserNode.getFloatFrequencyData()
    • AnalyserNode.getByteFrequencyData()
    • OscillatorNode frequency bias
    • DynamicsCompressorNode threshold bias
    • OfflineAudioContext rendering
    """

    def __init__(self):
        self._profiles: Dict[str, AudioProfile] = {}

    def get_profile(self, session_id: str) -> AudioProfile:
        if session_id not in self._profiles:
            self._profiles[session_id] = AudioProfile.generate(session_id)
        return self._profiles[session_id]

    def release_session(self, session_id: str) -> None:
        self._profiles.pop(session_id, None)

    def generate_injection_script(self, profile: AudioProfile) -> str:
        """Generate complete audio spoofing script."""
        return f"""
        (function() {{
            const SEED      = {profile.noise_seed};
            const INTENSITY = {profile.noise_intensity:.2e};
            const OSC_BIAS  = {profile.oscillator_bias:.2e};
            const COMP_BIAS = {profile.compressor_bias:.2e};

            function seededNoise(seed, index) {{
                const x = Math.sin(seed + index * 127.1) * 43758.5453;
                return x - Math.floor(x);
            }}

            // ── AudioBuffer.getChannelData ───────────────
            const origGetChannelData = AudioBuffer.prototype.getChannelData;
            AudioBuffer.prototype.getChannelData = function(channel) {{
                const data = origGetChannelData.call(this, channel);
                for (let i = 0; i < data.length; i++) {{
                    const noise = (seededNoise(SEED, i) - 0.5) * INTENSITY;
                    data[i] = Math.max(-1, Math.min(1, data[i] + noise));
                }}
                return data;
            }};

            // ── AnalyserNode patches ─────────────────────
            const origCreateAnalyser = AudioContext.prototype.createAnalyser;
            AudioContext.prototype.createAnalyser = function() {{
                const analyser = origCreateAnalyser.call(this);

                const origGetFloat = analyser.getFloatFrequencyData.bind(analyser);
                analyser.getFloatFrequencyData = function(array) {{
                    origGetFloat(array);
                    for (let i = 0; i < array.length; i++) {{
                        array[i] += (seededNoise(SEED, i) - 0.5) * 0.1;
                    }}
                }};

                const origGetByte = analyser.getByteFrequencyData.bind(analyser);
                analyser.getByteFrequencyData = function(array) {{
                    origGetByte(array);
                    for (let i = 0; i < array.length; i++) {{
                        const noise = Math.round((seededNoise(SEED, i) - 0.5) * 2);
                        array[i] = Math.max(0, Math.min(255, array[i] + noise));
                    }}
                }};

                return analyser;
            }};

            // ── AudioContext sampleRate ──────────────────
            const OrigAudioContext = AudioContext;
            const origSampleRateDesc = Object.getOwnPropertyDescriptor(
                AudioContext.prototype, 'sampleRate'
            );
            if (origSampleRateDesc) {{
                Object.defineProperty(AudioContext.prototype, 'sampleRate', {{
                    get: function() {{
                        return {profile.sample_rate};
                    }},
                    configurable: true,
                }});
            }}

            // ── OscillatorNode bias ──────────────────────
            const origCreateOscillator = AudioContext.prototype.createOscillator;
            AudioContext.prototype.createOscillator = function() {{
                const osc = origCreateOscillator.call(this);
                if (osc.frequency && osc.frequency.value !== undefined) {{
                    const origFreq = osc.frequency.value;
                    Object.defineProperty(osc.frequency, 'value', {{
                        get: () => origFreq + OSC_BIAS,
                        set: (v) => {{ origFreq; }},
                        configurable: true,
                    }});
                }}
                return osc;
            }};

            // ── OfflineAudioContext ──────────────────────
            const OrigOffline = OfflineAudioContext;
            if (OrigOffline) {{
                const origStartRendering = OrigOffline.prototype.startRendering;
                OrigOffline.prototype.startRendering = function() {{
                    return origStartRendering.call(this).then(buffer => {{
                        for (let ch = 0; ch < buffer.numberOfChannels; ch++) {{
                            const data = origGetChannelData.call(buffer, ch);
                            for (let i = 0; i < Math.min(data.length, 500); i++) {{
                                data[i] += (seededNoise(SEED, ch * 1000 + i) - 0.5)
                                           * INTENSITY;
                            }}
                        }}
                        return buffer;
                    }});
                }};
            }}
        }})();
        """

    async def apply_to_driver(
        self,
        driver:     Any,
        session_id: str,
    ) -> bool:
        """Apply audio spoofing to browser driver."""
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
                f"[AudioSpoofer] Applied to session: {session_id[:8]}"
            )
            return True
        except Exception as exc:
            logger.error(f"[AudioSpoofer] Apply error: {exc}")
            return False