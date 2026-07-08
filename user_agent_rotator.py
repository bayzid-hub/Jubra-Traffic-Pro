"""
Jubra Traffic Pro - Intelligent User Agent Rotator
Real Chrome/Firefox/Safari UAs with version weighting,
platform consistency, and usage tracking.
"""

import random
import time
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from collections import defaultdict, deque

logger = logging.getLogger(__name__)


@dataclass
class UserAgent:
    """A user agent string with metadata."""
    ua_string:      str
    browser:        str
    browser_version: str
    os_family:      str
    os_version:     str
    device_type:    str             # desktop, mobile, tablet
    market_share:   float           = 1.0
    usage_count:    int             = 0
    last_used:      float           = 0.0
    success_rate:   float           = 1.0

    @property
    def major_version(self) -> str:
        return self.browser_version.split(".")[0]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ua_string":     self.ua_string,
            "browser":       self.browser,
            "version":       self.browser_version,
            "os":            self.os_family,
            "device":        self.device_type,
            "market_share":  self.market_share,
            "success_rate":  round(self.success_rate, 4),
        }


class UserAgentRotator:
    """
    Intelligent User Agent rotation with:
    ─────────────────────────────────────────────────────
    • Real-world market share weighting
    • Platform consistency enforcement
    • Version freshness tracking
    • Per-session UA binding
    • Success rate adaptive selection
    • File-based UA database loading
    • Anti-pattern detection avoidance
    """

    # Real Chrome UAs (2024) with approximate market share weights
    CHROME_DESKTOP_UAS = [
        UserAgent(
            ua_string = (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.6422.142 Safari/537.36"
            ),
            browser="Chrome", browser_version="125.0.6422.142",
            os_family="Windows", os_version="10.0",
            device_type="desktop", market_share=12.5,
        ),
        UserAgent(
            ua_string = (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.6367.82 Safari/537.36"
            ),
            browser="Chrome", browser_version="124.0.6367.82",
            os_family="Windows", os_version="10.0",
            device_type="desktop", market_share=10.2,
        ),
        UserAgent(
            ua_string = (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/123.0.6312.122 Safari/537.36"
            ),
            browser="Chrome", browser_version="123.0.6312.122",
            os_family="Windows", os_version="10.0",
            device_type="desktop", market_share=8.1,
        ),
        UserAgent(
            ua_string = (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.6422.142 Safari/537.36"
            ),
            browser="Chrome", browser_version="125.0.6422.142",
            os_family="Mac", os_version="10.15.7",
            device_type="desktop", market_share=7.8,
        ),
        UserAgent(
            ua_string = (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.6367.82 Safari/537.36"
            ),
            browser="Chrome", browser_version="124.0.6367.82",
            os_family="Mac", os_version="10.15.7",
            device_type="desktop", market_share=6.4,
        ),
        UserAgent(
            ua_string = (
                "Mozilla/5.0 (Windows NT 11.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.6422.142 Safari/537.36"
            ),
            browser="Chrome", browser_version="125.0.6422.142",
            os_family="Windows", os_version="11.0",
            device_type="desktop", market_share=9.3,
        ),
        UserAgent(
            ua_string = (
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.6422.142 Safari/537.36"
            ),
            browser="Chrome", browser_version="125.0.6422.142",
            os_family="Linux", os_version="x86_64",
            device_type="desktop", market_share=3.2,
        ),
    ]

    CHROME_MOBILE_UAS = [
        UserAgent(
            ua_string = (
                "Mozilla/5.0 (Linux; Android 13; Pixel 7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.6422.142 Mobile Safari/537.36"
            ),
            browser="Chrome Mobile", browser_version="125.0.6422.142",
            os_family="Android", os_version="13",
            device_type="mobile", market_share=8.4,
        ),
        UserAgent(
            ua_string = (
                "Mozilla/5.0 (Linux; Android 12; SM-G991B) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.6367.82 Mobile Safari/537.36"
            ),
            browser="Chrome Mobile", browser_version="124.0.6367.82",
            os_family="Android", os_version="12",
            device_type="mobile", market_share=7.1,
        ),
        UserAgent(
            ua_string = (
                "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                "CriOS/125.0.6422.80 Mobile/15E148 Safari/604.1"
            ),
            browser="Chrome iOS", browser_version="125.0.6422.80",
            os_family="iOS", os_version="17.4",
            device_type="mobile", market_share=5.9,
        ),
        UserAgent(
            ua_string = (
                "Mozilla/5.0 (Linux; Android 14; Pixel 8) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.6422.142 Mobile Safari/537.36"
            ),
            browser="Chrome Mobile", browser_version="125.0.6422.142",
            os_family="Android", os_version="14",
            device_type="mobile", market_share=6.8,
        ),
    ]

    SAFARI_UAS = [
        UserAgent(
            ua_string = (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                "Version/17.4.1 Safari/605.1.15"
            ),
            browser="Safari", browser_version="17.4.1",
            os_family="Mac", os_version="14.4.1",
            device_type="desktop", market_share=4.2,
        ),
        UserAgent(
            ua_string = (
                "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4_1 like Mac OS X) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                "Version/17.4.1 Mobile/15E148 Safari/604.1"
            ),
            browser="Mobile Safari", browser_version="17.4.1",
            os_family="iOS", os_version="17.4.1",
            device_type="mobile", market_share=6.1,
        ),
    ]

    FIREFOX_UAS = [
        UserAgent(
            ua_string = (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) "
                "Gecko/20100101 Firefox/126.0"
            ),
            browser="Firefox", browser_version="126.0",
            os_family="Windows", os_version="10.0",
            device_type="desktop", market_share=2.8,
        ),
        UserAgent(
            ua_string = (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 14.4; rv:126.0) "
                "Gecko/20100101 Firefox/126.0"
            ),
            browser="Firefox", browser_version="126.0",
            os_family="Mac", os_version="14.4",
            device_type="desktop", market_share=1.9,
        ),
    ]

    EDGE_UAS = [
        UserAgent(
            ua_string = (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36 Edg/125.0.0.0"
            ),
            browser="Edge", browser_version="125.0.0.0",
            os_family="Windows", os_version="10.0",
            device_type="desktop", market_share=3.6,
        ),
    ]

    def __init__(
        self,
        ua_file:                Optional[str]   = None,
        desktop_ratio:          float           = 0.65,
        mobile_ratio:           float           = 0.30,
        tablet_ratio:           float           = 0.05,
        use_market_weights:     bool            = True,
        recent_window:          int             = 20,
    ):
        self._desktop_ratio         = desktop_ratio
        self._mobile_ratio          = mobile_ratio
        self._tablet_ratio          = tablet_ratio
        self._use_market_weights    = use_market_weights

        # Build UA database
        self._desktop_uas:  List[UserAgent] = []
        self._mobile_uas:   List[UserAgent] = []
        self._all_uas:      List[UserAgent] = []
        self._session_uas:  Dict[str, UserAgent] = {}
        self._recent_used:  deque = deque(maxlen=recent_window)

        self._load_builtin_uas()

        if ua_file:
            self._load_from_file(ua_file)

        logger.info(
            f"[UserAgentRotator] Initialized: "
            f"desktop={len(self._desktop_uas)}, "
            f"mobile={len(self._mobile_uas)}"
        )

    def _load_builtin_uas(self) -> None:
        """Load all built-in user agents."""
        desktop = (
            self.CHROME_DESKTOP_UAS +
            self.SAFARI_UAS[:1] +
            self.FIREFOX_UAS +
            self.EDGE_UAS
        )
        mobile  = self.CHROME_MOBILE_UAS + self.SAFARI_UAS[1:]

        self._desktop_uas = desktop
        self._mobile_uas  = mobile
        self._all_uas     = desktop + mobile

    def _load_from_file(self, filepath: str) -> None:
        """Load additional UAs from JSON file."""
        path = Path(filepath)
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            for item in data:
                if isinstance(item, str):
                    ua = UserAgent(
                        ua_string    = item,
                        browser      = "Chrome",
                        browser_version = "125.0",
                        os_family    = "Windows",
                        os_version   = "10.0",
                        device_type  = "desktop",
                        market_share = 1.0,
                    )
                    self._desktop_uas.append(ua)
                    self._all_uas.append(ua)
                elif isinstance(item, dict):
                    ua = UserAgent(**item)
                    if ua.device_type == "mobile":
                        self._mobile_uas.append(ua)
                    else:
                        self._desktop_uas.append(ua)
                    self._all_uas.append(ua)

            logger.info(
                f"[UserAgentRotator] Loaded from file: {len(data)} UAs"
            )
        except Exception as exc:
            logger.error(f"[UserAgentRotator] Load error: {exc}")

    def get(
        self,
        session_id:     Optional[str]   = None,
        device_type:    Optional[str]   = None,
        os_family:      Optional[str]   = None,
        browser:        Optional[str]   = None,
    ) -> UserAgent:
        """
        Get a user agent with optional constraints.
        If session_id provided, returns same UA for the session.
        """
        # Return cached session UA
        if session_id and session_id in self._session_uas:
            return self._session_uas[session_id]

        # Determine pool
        if device_type == "mobile":
            pool = self._mobile_uas
        elif device_type == "desktop":
            pool = self._desktop_uas
        else:
            # Random device type based on ratio
            r = random.random()
            if r < self._desktop_ratio:
                pool = self._desktop_uas
            elif r < self._desktop_ratio + self._mobile_ratio:
                pool = self._mobile_uas
            else:
                pool = self._desktop_uas  # No tablet pool, fallback

        # Filter by OS
        if os_family:
            filtered = [
                u for u in pool
                if u.os_family.lower() == os_family.lower()
            ]
            if filtered:
                pool = filtered

        # Filter by browser
        if browser:
            filtered = [
                u for u in pool
                if browser.lower() in u.browser.lower()
            ]
            if filtered:
                pool = filtered

        if not pool:
            pool = self._all_uas

        # Avoid recently used
        recent_set  = set(u.ua_string for u in self._recent_used)
        candidates  = [u for u in pool if u.ua_string not in recent_set]
        if not candidates:
            candidates = pool

        # Weighted selection
        if self._use_market_weights:
            weights = [
                u.market_share * u.success_rate
                for u in candidates
            ]
            total = sum(weights)
            if total > 0:
                r = random.uniform(0, total)
                cumulative = 0.0
                selected   = candidates[-1]
                for ua, w in zip(candidates, weights):
                    cumulative += w
                    if r <= cumulative:
                        selected = ua
                        break
            else:
                selected = random.choice(candidates)
        else:
            selected = random.choice(candidates)

        # Update tracking
        selected.usage_count += 1
        selected.last_used    = time.monotonic()
        self._recent_used.append(selected)

        # Bind to session
        if session_id:
            self._session_uas[session_id] = selected

        return selected

    def get_string(
        self,
        session_id:     Optional[str]   = None,
        device_type:    Optional[str]   = None,
        os_family:      Optional[str]   = None,
    ) -> str:
        """Get just the UA string."""
        return self.get(session_id, device_type, os_family).ua_string

    def release_session(self, session_id: str) -> None:
        """Release session UA binding."""
        self._session_uas.pop(session_id, None)

    def record_outcome(self, session_id: str, success: bool) -> None:
        """Update success rate for session's UA."""
        ua = self._session_uas.get(session_id)
        if ua:
            ua.success_rate = (
                0.85 * ua.success_rate +
                0.15 * (1.0 if success else 0.0)
            )

    def get_stats(self) -> Dict[str, Any]:
        return {
            "total_uas":        len(self._all_uas),
            "desktop_uas":      len(self._desktop_uas),
            "mobile_uas":       len(self._mobile_uas),
            "active_sessions":  len(self._session_uas),
            "top_used": sorted(
                [u.to_dict() for u in self._all_uas],
                key=lambda x: x.get("market_share", 0),
                reverse=True,
            )[:5],
        }