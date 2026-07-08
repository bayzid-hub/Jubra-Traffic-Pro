"""
Jubra Traffic Pro - Keyboard Dynamics (nodriver Edition)
WPM-based typing using nodriver's native CDP Input domain.
No Selenium Keys dependency.
"""

import asyncio
import random
import time
import math
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
from collections import deque

try:
    import nodriver as uc
    HAS_NODRIVER = True
except ImportError:
    HAS_NODRIVER = False

logger = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Keystroke
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@dataclass
class Keystroke:
    """A single keystroke with biometric timing."""
    key:        str
    dwell_ms:   float
    flight_ms:  float
    is_typo:    bool    = False
    timestamp:  float   = field(default_factory=time.monotonic)


@dataclass
class TypingSession:
    """Analytics for a complete typing session."""
    text:               str
    keystrokes:         List[Keystroke]
    start_time:         float
    end_time:           float
    typo_count:         int     = 0
    correction_count:   int     = 0
    target_wpm:         int     = 60

    @property
    def actual_wpm(self) -> float:
        duration_min = (self.end_time - self.start_time) / 60
        if duration_min <= 0:
            return 0.0
        return len(self.text.split()) / duration_min

    @property
    def accuracy(self) -> float:
        if not self.keystrokes:
            return 1.0
        errors = sum(1 for k in self.keystrokes if k.is_typo)
        return max(0.0, 1.0 - errors / len(self.keystrokes))


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Key Timing Model
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class KeyTimingModel:
    """QWERTY keyboard timing model."""

    KEY_POSITIONS: Dict[str, Tuple[int,int]] = {
        'q':(1,0),'w':(1,1),'e':(1,2),'r':(1,3),'t':(1,4),
        'y':(1,5),'u':(1,6),'i':(1,7),'o':(1,8),'p':(1,9),
        'a':(2,0),'s':(2,1),'d':(2,2),'f':(2,3),'g':(2,4),
        'h':(2,5),'j':(2,6),'k':(2,7),'l':(2,8),
        'z':(3,0),'x':(3,1),'c':(3,2),'v':(3,3),'b':(3,4),
        'n':(3,5),'m':(3,6),' ':(4,5),
    }

    LEFT_HAND   = set('qwertasdfgzxcvb')
    RIGHT_HAND  = set('yuiophjklnm')

    FAST_BIGRAMS = {
        'th','he','in','er','an','en','on','st','nt',
        'es','at','nd','ou','re','ng','or','te','al',
    }

    def __init__(self, target_wpm: int = 60):
        self.target_wpm     = target_wpm
        self._chars_per_s   = target_wpm * 5 / 60
        self._base_char_ms  = 1000 / self._chars_per_s
        self._cache:        Dict[str, float] = {}

    def get_dwell_ms(self, key: str) -> float:
        base    = self._base_char_ms * 0.45
        sigma   = 0.25
        dwell   = base * math.exp(random.gauss(0, sigma))
        if key in ('\n', '\t', '\b'):
            dwell *= random.uniform(1.2, 1.8)
        elif key.isupper() or key in '!@#$%^&*':
            dwell *= random.uniform(1.1, 1.4)
        return max(30, min(350, dwell))

    def get_flight_ms(self, curr: str, nxt: str) -> float:
        if not nxt:
            return 0.0
        cache_key = f"{curr}{nxt}"
        if cache_key in self._cache:
            return self._cache[cache_key] * random.uniform(0.85, 1.15)

        base    = self._base_char_ms * 0.55
        dist_f  = self._distance_factor(curr.lower(), nxt.lower())

        curr_l  = curr.lower() in self.LEFT_HAND
        nxt_l   = nxt.lower()  in self.LEFT_HAND
        hand_f  = (
            random.uniform(1.05, 1.20)
            if curr_l == nxt_l
            else random.uniform(0.85, 0.95)
        )
        bigram_f = 0.85 if (curr+nxt).lower() in self.FAST_BIGRAMS else 1.0
        flight   = base * dist_f * hand_f * bigram_f
        flight  *= math.exp(random.gauss(0, 0.20))

        result              = max(40, min(500, flight))
        self._cache[cache_key] = result
        return result

    def _distance_factor(self, k1: str, k2: str) -> float:
        p1 = self.KEY_POSITIONS.get(k1)
        p2 = self.KEY_POSITIONS.get(k2)
        if not p1 or not p2:
            return 1.0
        dist = math.sqrt(
            (p1[0]-p2[0])**2 + (p1[1]-p2[1])**2
        )
        if   dist == 0:   return 0.7
        elif dist <= 1.5: return 0.85
        elif dist <= 3.0: return 1.0
        elif dist <= 5.0: return 1.15
        else:             return 1.30

    def update_wpm(self, wpm: int) -> None:
        self.target_wpm     = wpm
        self._chars_per_s   = wpm * 5 / 60
        self._base_char_ms  = 1000 / self._chars_per_s
        self._cache.clear()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Keyboard Dynamics (nodriver)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class KeyboardDynamics:
    """
    nodriver Keyboard Dynamics Engine.

    Uses CDP Input.dispatchKeyEvent directly:
    ─────────────────────────────────────────────────────
    • No Selenium Keys dependency
    • Real CDP keyboard events
    • WPM-based dwell/flight timing
    • Natural typo injection and correction
    • Burst typing with micro-pauses
    • Password mode (slower, no typos)
    """

    ADJACENT_KEYS: Dict[str, List[str]] = {
        'a':['q','w','s','z'],      'b':['v','g','h','n'],
        'c':['x','d','f','v'],      'd':['s','e','r','f','c','x'],
        'e':['w','r','d','s'],      'f':['d','r','t','g','v','c'],
        'g':['f','t','y','h','b'],  'h':['g','y','u','j','n','b'],
        'i':['u','o','k','j'],      'j':['h','u','i','k','n','m'],
        'k':['j','i','o','l','m'],  'l':['k','o','p',';'],
        'm':['n','j','k'],          'n':['b','h','j','m'],
        'o':['i','p','l','k'],      'p':['o','l'],
        'q':['w','a'],              'r':['e','t','f','d'],
        's':['a','w','e','d','x'],  't':['r','y','g','f'],
        'u':['y','i','j','h'],      'v':['c','f','g','b'],
        'w':['q','e','s','a'],      'x':['z','s','d','c'],
        'y':['t','u','h','g'],      'z':['a','s','x'],
    }

    # CDP key code map for special keys
    SPECIAL_KEYS: Dict[str, Dict[str, Any]] = {
        '\n':   {"key": "Enter",    "code": "Enter",     "keyCode": 13},
        '\t':   {"key": "Tab",      "code": "Tab",       "keyCode": 9},
        '\b':   {"key": "Backspace","code": "Backspace",  "keyCode": 8},
        ' ':    {"key": " ",        "code": "Space",     "keyCode": 32},
        'ESC':  {"key": "Escape",   "code": "Escape",    "keyCode": 27},
        'TAB':  {"key": "Tab",      "code": "Tab",       "keyCode": 9},
        'ENTER':{"key": "Enter",    "code": "Enter",     "keyCode": 13},
        'UP':   {"key": "ArrowUp",  "code": "ArrowUp",   "keyCode": 38},
        'DOWN': {"key": "ArrowDown","code": "ArrowDown", "keyCode": 40},
        'LEFT': {"key": "ArrowLeft","code": "ArrowLeft", "keyCode": 37},
        'RIGHT':{"key": "ArrowRight","code":"ArrowRight","keyCode": 39},
        'DEL':  {"key": "Delete",   "code": "Delete",    "keyCode": 46},
        'HOME': {"key": "Home",     "code": "Home",      "keyCode": 36},
        'END':  {"key": "End",      "code": "End",       "keyCode": 35},
    }

    def __init__(
        self,
        page:               Any,    # nodriver Tab
        wpm_min:            int     = 40,
        wpm_max:            int     = 80,
        typo_rate:          float   = 0.04,
        correction_rate:    float   = 0.92,
        burst_typing:       bool    = True,
        fatigue_factor:     float   = 0.05,
    ):
        self._page              = page
        self._wpm_min           = wpm_min
        self._wpm_max           = wpm_max
        self._typo_rate         = typo_rate
        self._correction_rate   = correction_rate
        self._burst_typing      = burst_typing
        self._fatigue_factor    = fatigue_factor

        self._current_wpm   = random.randint(wpm_min, wpm_max)
        self._timing_model  = KeyTimingModel(self._current_wpm)

        # Stats
        self._total_keystrokes: int     = 0
        self._total_typos:      int     = 0
        self._sessions:         List    = []
        self._burst_count:      int     = 0
        self._chars_since_pause: int    = 0

    # ── CDP Key Event Dispatcher ───────────────────────────

    async def _dispatch_key_event(
        self,
        event_type: str,
        key:        str,
        code:       str         = "",
        key_code:   int         = 0,
        modifiers:  int         = 0,
        text:       str         = "",
    ) -> None:
        """Dispatch keyboard event via CDP Input domain."""
        try:
            await self._page.send(
                uc.cdp.input_.dispatch_key_event(
                    type_           = event_type,
                    key             = key,
                    code            = code or f"Key{key.upper()}",
                    native_virtual_key_code = key_code,
                    windows_virtual_key_code = key_code,
                    modifiers       = modifiers,
                    text            = text if event_type == "keyDown" else "",
                )
            )
        except Exception as exc:
            logger.debug(f"[KeyboardDynamics] Key event error: {exc}")

    # ── Core Typing ────────────────────────────────────────

    async def type_text(
        self,
        text:       str,
        element:    Optional[Any]   = None,
        clear:      bool            = False,
        password:   bool            = False,
    ) -> TypingSession:
        """Type text with realistic keystroke timing."""
        if not text:
            return TypingSession(
                text="", keystrokes=[],
                start_time=time.monotonic(),
                end_time=time.monotonic(),
            )

        # Focus element if provided
        if element:
            await element.click()
            await asyncio.sleep(random.uniform(0.1, 0.3))

        # Clear if requested
        if clear:
            await self._select_all_and_delete()
            await asyncio.sleep(random.uniform(0.05, 0.15))

        # Password mode adjustments
        if password:
            session_typo_rate = 0.0
            self._timing_model.update_wpm(
                max(25, self._current_wpm // 2)
            )
        else:
            session_typo_rate = self._typo_rate

        start_time  = time.monotonic()
        keystrokes: List[Keystroke] = []
        typo_count  = 0
        corr_count  = 0
        i           = 0

        while i < len(text):
            char = text[i]

            # Fatigue
            self._apply_fatigue(i, len(text))

            # Thinking pause at spaces
            if char == ' ' and random.random() < 0.10:
                await asyncio.sleep(random.uniform(0.2, 0.8))

            # Burst management
            if self._burst_typing:
                await self._manage_burst()

            # Typo logic
            make_typo = (
                random.random() < session_typo_rate and
                char.isalpha() and
                len(text) > 3 and
                not password
            )

            if make_typo:
                typo_seq = self._generate_typo(text, i)
                for tc in typo_seq:
                    ks = await self._type_char(tc, text, i)
                    if ks:
                        ks.is_typo = True
                        keystrokes.append(ks)
                        typo_count += 1

                # Correction
                if random.random() < self._correction_rate:
                    await asyncio.sleep(random.uniform(0.15, 0.55))
                    for _ in range(len(typo_seq)):
                        bs_ks = await self._type_char('\b', text, i)
                        if bs_ks:
                            keystrokes.append(bs_ks)
                            corr_count += 1

                    ks = await self._type_char(char, text, i)
                    if ks:
                        keystrokes.append(ks)
            else:
                ks = await self._type_char(char, text, i)
                if ks:
                    keystrokes.append(ks)

            i += 1

        end_time = time.monotonic()

        if password:
            self._timing_model.update_wpm(self._current_wpm)

        session = TypingSession(
            text            = text,
            keystrokes      = keystrokes,
            start_time      = start_time,
            end_time        = end_time,
            typo_count      = typo_count,
            correction_count = corr_count,
            target_wpm      = self._current_wpm,
        )
        self._sessions.append(session)
        self._total_keystrokes  += len(keystrokes)
        self._total_typos       += typo_count

        logger.debug(
            f"[KeyboardDynamics] Typed: {len(text)} chars | "
            f"wpm={session.actual_wpm:.0f} | "
            f"typos={typo_count}"
        )
        return session

    async def _type_char(
        self,
        char:   str,
        text:   str,
        index:  int,
    ) -> Optional[Keystroke]:
        """Type a single character via CDP."""
        next_char   = text[index+1] if index+1 < len(text) else ""
        dwell_ms    = self._timing_model.get_dwell_ms(char)
        flight_ms   = self._timing_model.get_flight_ms(char, next_char)

        try:
            # Handle special keys
            if char in self.SPECIAL_KEYS:
                spec    = self.SPECIAL_KEYS[char]
                await self._dispatch_key_event(
                    "keyDown",
                    key         = spec["key"],
                    code        = spec["code"],
                    key_code    = spec["keyCode"],
                )
                await asyncio.sleep(dwell_ms / 1000)
                await self._dispatch_key_event(
                    "keyUp",
                    key         = spec["key"],
                    code        = spec["code"],
                    key_code    = spec["keyCode"],
                )
            else:
                # Regular character
                key_code = ord(char.upper()) if char.isalpha() else ord(char)

                # Shift modifier for uppercase
                modifiers = 8 if char.isupper() else 0

                # keyDown
                await self._dispatch_key_event(
                    "keyDown",
                    key         = char,
                    code        = f"Key{char.upper()}" if char.isalpha() else "",
                    key_code    = key_code,
                    modifiers   = modifiers,
                    text        = char,
                )
                await asyncio.sleep(dwell_ms / 1000)

                # char (inserts text)
                await self._dispatch_key_event(
                    "char",
                    key         = char,
                    text        = char,
                    modifiers   = modifiers,
                )

                # keyUp
                await self._dispatch_key_event(
                    "keyUp",
                    key         = char,
                    code        = f"Key{char.upper()}" if char.isalpha() else "",
                    key_code    = key_code,
                    modifiers   = modifiers,
                )

            # Flight time
            if flight_ms > 0:
                await asyncio.sleep(flight_ms / 1000)

            return Keystroke(
                key         = char,
                dwell_ms    = dwell_ms,
                flight_ms   = flight_ms,
            )

        except Exception as exc:
            logger.debug(f"[KeyboardDynamics] Type char error: {exc}")
            return None

    async def press_key(
        self,
        key:        str,
        modifiers:  Optional[List[str]] = None,
    ) -> None:
        """Press a special/function key."""
        mods    = modifiers or []
        mod_val = 0

        # Modifier codes
        mod_map = {"ctrl": 2, "shift": 8, "alt": 1, "meta": 4}
        for mod in mods:
            mod_val |= mod_map.get(mod.lower(), 0)

        spec = self.SPECIAL_KEYS.get(key.upper(), {
            "key":     key,
            "code":    f"Key{key.upper()}",
            "keyCode": ord(key.upper()) if len(key) == 1 else 0,
        })

        await asyncio.sleep(random.uniform(0.05, 0.15))
        await self._dispatch_key_event(
            "keyDown",
            key         = spec["key"],
            code        = spec["code"],
            key_code    = spec["keyCode"],
            modifiers   = mod_val,
        )
        await asyncio.sleep(random.uniform(0.05, 0.12))
        await self._dispatch_key_event(
            "keyUp",
            key         = spec["key"],
            code        = spec["code"],
            key_code    = spec["keyCode"],
            modifiers   = mod_val,
        )

    async def type_search_query(
        self,
        query:      str,
        element:    Optional[Any] = None,
    ) -> TypingSession:
        """Type a search query with casual typing style."""
        await asyncio.sleep(random.uniform(0.3, 1.2))
        old_typo        = self._typo_rate
        self._typo_rate = min(0.08, self._typo_rate * 1.5)
        session         = await self.type_text(query, element)
        self._typo_rate = old_typo
        await asyncio.sleep(random.uniform(0.2, 0.8))
        return session

    # ── Helpers ────────────────────────────────────────────

    async def _select_all_and_delete(self) -> None:
        """Select all text and delete via CDP."""
        # Ctrl+A
        await self._dispatch_key_event(
            "keyDown", "a", "KeyA", 65, modifiers=2
        )
        await self._dispatch_key_event(
            "keyUp",   "a", "KeyA", 65, modifiers=2
        )
        await asyncio.sleep(0.05)
        # Delete
        await self._dispatch_key_event(
            "keyDown", "Delete", "Delete", 46
        )
        await self._dispatch_key_event(
            "keyUp",   "Delete", "Delete", 46
        )

    def _generate_typo(self, text: str, index: int) -> List[str]:
        """Generate a typo sequence."""
        char    = text[index]
        r       = random.random()

        if r < 0.40 and index+1 < len(text):
            return [text[index+1], char]
        elif r < 0.70:
            adj = self.ADJACENT_KEYS.get(char.lower(), [char])
            w   = random.choice(adj)
            return [w.upper() if char.isupper() else w]
        elif r < 0.90 and index+1 < len(text):
            return [text[index+1]]
        else:
            return [char, char]

    def _apply_fatigue(self, i: int, total: int) -> None:
        if total < 50:
            return
        if i/total > 0.7 and random.random() < self._fatigue_factor:
            new_wpm = max(
                self._wpm_min,
                self._current_wpm - random.randint(1, 5),
            )
            if new_wpm != self._current_wpm:
                self._current_wpm = new_wpm
                self._timing_model.update_wpm(new_wpm)

    async def _manage_burst(self) -> None:
        self._chars_since_pause += 1
        if self._chars_since_pause >= random.randint(5, 25):
            await asyncio.sleep(random.uniform(0.08, 0.35))
            self._chars_since_pause = 0
            self._burst_count += 1

    def update_page(self, page: Any) -> None:
        """Update page reference after navigation."""
        self._page = page

    def get_stats(self) -> Dict[str, Any]:
        return {
            "engine":           "nodriver_cdp",
            "total_keystrokes": self._total_keystrokes,
            "total_typos":      self._total_typos,
            "current_wpm":      self._current_wpm,
            "burst_count":      self._burst_count,
            "sessions":         len(self._sessions),
        }