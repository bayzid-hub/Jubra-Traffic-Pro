"""
Jubra Traffic Pro - Advanced Session Manager
Complete session lifecycle management with fingerprint binding,
resource pooling, health monitoring, and distributed coordination.
"""

import asyncio
import time
import uuid
import json
import random
import hashlib
import logging
import weakref
from enum import Enum, auto
from dataclasses import dataclass, field
from typing import (
    Any, Dict, List, Optional, Set, Tuple,
    AsyncIterator, Callable, Union
)
from collections import defaultdict, deque
from contextlib import asynccontextmanager

from core.exceptions import (
    SessionError,
    SessionCreationError,
    SessionExpiredError,
    SessionLimitError,
    SessionCorruptedError,
    ProxyError,
    BrowserError,
    ErrorContext,
    Severity,
)
from core.event_bus import (
    EventBus,
    EventCategory,
    EventPriority,
    Event,
    get_event_bus,
)
from core.config_manager import ConfigManager

logger = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Session State Machine
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class SessionState(Enum):
    """
    Complete session lifecycle state machine.

    Transitions:
    ────────────────────────────────────────────────
    INITIALIZING → READY
    READY        → ACTIVE | EXPIRED | DESTROYED
    ACTIVE       → IDLE | COMPLETING | FAILED | DETECTED
    IDLE         → ACTIVE | COMPLETING | EXPIRED
    COMPLETING   → COMPLETED | FAILED
    COMPLETED    → RECYCLING | DESTROYED
    RECYCLING    → READY
    FAILED       → RECOVERING | DESTROYED
    RECOVERING   → READY | DESTROYED
    DETECTED     → DESTROYED
    EXPIRED      → DESTROYED
    DESTROYED    → (terminal)
    """
    INITIALIZING = auto()
    READY        = auto()
    ACTIVE       = auto()
    IDLE         = auto()
    COMPLETING   = auto()
    COMPLETED    = auto()
    RECYCLING    = auto()
    FAILED       = auto()
    RECOVERING   = auto()
    DETECTED     = auto()
    EXPIRED      = auto()
    DESTROYED    = auto()


# Valid state transitions
SESSION_TRANSITIONS: Dict[SessionState, Set[SessionState]] = {
    SessionState.INITIALIZING: {SessionState.READY, SessionState.DESTROYED},
    SessionState.READY:        {SessionState.ACTIVE, SessionState.EXPIRED, SessionState.DESTROYED},
    SessionState.ACTIVE:       {SessionState.IDLE, SessionState.COMPLETING, SessionState.FAILED, SessionState.DETECTED},
    SessionState.IDLE:         {SessionState.ACTIVE, SessionState.COMPLETING, SessionState.EXPIRED},
    SessionState.COMPLETING:   {SessionState.COMPLETED, SessionState.FAILED},
    SessionState.COMPLETED:    {SessionState.RECYCLING, SessionState.DESTROYED},
    SessionState.RECYCLING:    {SessionState.READY, SessionState.DESTROYED},
    SessionState.FAILED:       {SessionState.RECOVERING, SessionState.DESTROYED},
    SessionState.RECOVERING:   {SessionState.READY, SessionState.DESTROYED},
    SessionState.DETECTED:     {SessionState.DESTROYED},
    SessionState.EXPIRED:      {SessionState.DESTROYED},
    SessionState.DESTROYED:    set(),
}


class TrafficType(Enum):
    """Traffic source type for this session."""
    ORGANIC  = "organic"
    SOCIAL   = "social"
    DIRECT   = "direct"
    REFERRAL = "referral"
    EMAIL    = "email"
    PAID     = "paid"


class DeviceType(Enum):
    """Device type for this session."""
    DESKTOP = "desktop"
    MOBILE  = "mobile"
    TABLET  = "tablet"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Session Identity
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@dataclass
class SessionIdentity:
    """
    Complete identity profile for a session.
    Binds proxy, fingerprint, user-agent, and behavioral parameters.
    All fields are immutable after creation to ensure consistency.
    """
    # Core identity
    session_id:         str
    fingerprint_id:     str
    proxy_id:           Optional[str]

    # Browser identity
    user_agent:         str
    browser_version:    str
    os_platform:        str
    os_version:         str
    device_type:        DeviceType
    viewport_width:     int
    viewport_height:    int
    color_depth:        int
    pixel_ratio:        float

    # Network identity
    proxy_address:      Optional[str]
    proxy_protocol:     Optional[str]
    proxy_country:      str
    proxy_city:         str
    proxy_isp:          str
    proxy_asn:          str

    # Language / Locale
    language:           str
    languages:          List[str]
    timezone:           str
    locale:             str

    # Canvas / WebGL fingerprint
    canvas_noise_seed:  int
    webgl_renderer:     str
    webgl_vendor:       str

    # Audio fingerprint
    audio_noise_seed:   int

    # TLS identity
    tls_fingerprint_id: str
    ja3_hash:           str

    # Traffic identity
    traffic_type:       TrafficType
    referrer:           str
    entry_url:          str
    search_keyword:     Optional[str]
    search_engine:      Optional[str]

    # Behavioral profile
    typing_wpm:         int
    mouse_speed:        float          # pixels per second
    scroll_speed:       str
    read_speed:         float          # words per second
    idle_frequency:     float

    # Timestamps
    created_at:         float = field(default_factory=time.monotonic)

    def __post_init__(self):
        # Compute combined identity hash
        raw = (
            f"{self.user_agent}:{self.fingerprint_id}:"
            f"{self.canvas_noise_seed}:{self.audio_noise_seed}:"
            f"{self.tls_fingerprint_id}"
        )
        self.identity_hash = hashlib.sha256(raw.encode()).hexdigest()[:16]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id":         self.session_id,
            "fingerprint_id":     self.fingerprint_id,
            "proxy_id":           self.proxy_id,
            "user_agent":         self.user_agent,
            "browser_version":    self.browser_version,
            "os_platform":        self.os_platform,
            "os_version":         self.os_version,
            "device_type":        self.device_type.value,
            "viewport":           f"{self.viewport_width}x{self.viewport_height}",
            "proxy_country":      self.proxy_country,
            "language":           self.language,
            "timezone":           self.timezone,
            "traffic_type":       self.traffic_type.value,
            "search_keyword":     self.search_keyword,
            "search_engine":      self.search_engine,
            "typing_wpm":         self.typing_wpm,
            "identity_hash":      self.identity_hash,
        }

    def __repr__(self) -> str:
        return (
            f"SessionIdentity("
            f"id={self.session_id[:8]}, "
            f"ua={self.user_agent[:40]!r}, "
            f"proxy={self.proxy_country}, "
            f"type={self.traffic_type.value})"
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Session Metrics
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@dataclass
class SessionMetrics:
    """
    Real-time metrics collected during a session.
    Used for analytics reporting and behavioral analysis.
    """
    # Page metrics
    pages_visited:          int   = 0
    total_clicks:           int   = 0
    total_scrolls:          int   = 0
    total_form_fills:       int   = 0
    total_keystrokes:       int   = 0
    total_mouse_moves:      int   = 0

    # Timing metrics (seconds)
    time_on_site:           float = 0.0
    time_on_current_page:   float = 0.0
    avg_time_per_page:      float = 0.0
    idle_time:              float = 0.0

    # Network metrics
    requests_made:          int   = 0
    bytes_downloaded:       int   = 0
    captchas_encountered:   int   = 0
    captchas_solved:        int   = 0

    # Goal metrics
    goals_reached:          int   = 0
    conversions:            int   = 0
    bounce:                 bool  = False

    # Detection metrics
    detection_signals:      int   = 0
    detection_evaded:       int   = 0
    bot_score:              float = 0.0  # 0.0=human, 1.0=bot

    # Error tracking
    page_errors:            int   = 0
    js_errors:              int   = 0
    network_errors:         int   = 0

    # History
    visited_urls:           List[str]         = field(default_factory=list)
    page_timings:           List[float]       = field(default_factory=list)
    click_positions:        List[Tuple[int,int]] = field(default_factory=list)
    scroll_depths:          List[float]       = field(default_factory=list)

    def record_page_visit(self, url: str, time_spent: float) -> None:
        self.pages_visited += 1
        self.visited_urls.append(url)
        self.page_timings.append(time_spent)
        total = sum(self.page_timings)
        self.avg_time_per_page = total / len(self.page_timings)
        self.time_on_site += time_spent

    def record_click(self, x: int, y: int) -> None:
        self.total_clicks += 1
        self.click_positions.append((x, y))

    def record_scroll(self, depth: float) -> None:
        self.total_scrolls += 1
        self.scroll_depths.append(depth)

    @property
    def avg_scroll_depth(self) -> float:
        if not self.scroll_depths:
            return 0.0
        return sum(self.scroll_depths) / len(self.scroll_depths)

    @property
    def engagement_score(self) -> float:
        """
        Composite engagement score 0.0-1.0.
        Higher = more engaged human-like behavior.
        """
        score = 0.0
        if self.pages_visited > 1:
            score += min(self.pages_visited / 10, 0.25)
        if self.time_on_site > 30:
            score += min(self.time_on_site / 300, 0.25)
        if self.total_clicks > 2:
            score += min(self.total_clicks / 20, 0.20)
        if self.avg_scroll_depth > 0.3:
            score += min(self.avg_scroll_depth, 0.15)
        if self.total_mouse_moves > 50:
            score += min(self.total_mouse_moves / 1000, 0.15)
        return min(score, 1.0)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "pages_visited":        self.pages_visited,
            "total_clicks":         self.total_clicks,
            "total_scrolls":        self.total_scrolls,
            "time_on_site":         round(self.time_on_site, 2),
            "avg_time_per_page":    round(self.avg_time_per_page, 2),
            "idle_time":            round(self.idle_time, 2),
            "requests_made":        self.requests_made,
            "bytes_downloaded":     self.bytes_downloaded,
            "captchas_encountered": self.captchas_encountered,
            "captchas_solved":      self.captchas_solved,
            "goals_reached":        self.goals_reached,
            "conversions":          self.conversions,
            "bounce":               self.bounce,
            "detection_signals":    self.detection_signals,
            "bot_score":            round(self.bot_score, 4),
            "engagement_score":     round(self.engagement_score, 4),
            "visited_urls":         self.visited_urls[-20:],
            "avg_scroll_depth":     round(self.avg_scroll_depth, 4),
        }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Session Object
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class Session:
    """
    Core session object representing a single visitor session.

    Manages:
    ─────────────────────────────────────────────────
    • State machine with validated transitions
    • Resource handles (browser, proxy)
    • Identity binding (fingerprint, UA, geo)
    • Real-time metrics collection
    • Error history and recovery state
    • Correlation with traffic campaigns
    • Cookie / localStorage state
    • Custom session-scoped data store
    """

    def __init__(
        self,
        identity:       SessionIdentity,
        browser_id:     Optional[str]   = None,
        campaign_id:    Optional[str]   = None,
        max_duration:   float           = 600.0,
        max_pages:      int             = 20,
        correlation_id: Optional[str]   = None,
    ):
        self.session_id     = identity.session_id
        self.identity       = identity
        self.browser_id     = browser_id
        self.campaign_id    = campaign_id
        self.correlation_id = correlation_id or str(uuid.uuid4())
        self.max_duration   = max_duration
        self.max_pages      = max_pages

        # State
        self._state         = SessionState.INITIALIZING
        self._state_history: List[Tuple[SessionState, float]] = [
            (SessionState.INITIALIZING, time.monotonic())
        ]

        # Metrics
        self.metrics        = SessionMetrics()

        # Resources
        self._browser_driver: Any = None
        self._cookies:      Dict[str, str]  = {}
        self._local_storage: Dict[str, str] = {}
        self._session_storage: Dict[str, str] = {}

        # Session data store (arbitrary key-value for modules to use)
        self._data:         Dict[str, Any]  = {}

        # Errors
        self._errors:       List[Dict[str, Any]] = []
        self._recovery_attempts: int = 0
        self.max_recovery_attempts: int = 3

        # Timing
        self.created_at     = time.monotonic()
        self.started_at:    Optional[float] = None
        self.completed_at:  Optional[float] = None
        self.last_active:   float = time.monotonic()

        # Navigation history
        self._nav_history:  List[str] = []
        self.current_url:   str = identity.entry_url

        # Locks
        self._lock          = asyncio.Lock()
        self._state_lock    = asyncio.Lock()

        # Callbacks registered by subsystems
        self._on_destroy_callbacks: List[Callable] = []

        logger.debug(
            f"[Session] Created: {self.session_id[:8]} | "
            f"type={identity.traffic_type.value} | "
            f"device={identity.device_type.value}"
        )

    # ── State Machine ──────────────────────────────────────

    @property
    def state(self) -> SessionState:
        return self._state

    async def transition_to(self, new_state: SessionState) -> bool:
        """
        Attempt a state transition.
        Returns True if successful, raises on invalid transition.
        """
        async with self._state_lock:
            allowed = SESSION_TRANSITIONS.get(self._state, set())
            if new_state not in allowed:
                raise SessionCorruptedError(
                    session_id=self.session_id,
                    reason=(
                        f"Invalid transition: "
                        f"{self._state.name} → {new_state.name}"
                    ),
                    context=ErrorContext(
                        module="Session",
                        operation="transition_to",
                        session_id=self.session_id,
                    ),
                )

            old_state = self._state
            self._state = new_state
            self._state_history.append((new_state, time.monotonic()))
            self.last_active = time.monotonic()

            logger.debug(
                f"[Session] {self.session_id[:8]}: "
                f"{old_state.name} → {new_state.name}"
            )

            # Handle specific transition side-effects
            if new_state == SessionState.ACTIVE and self.started_at is None:
                self.started_at = time.monotonic()
            elif new_state in (
                SessionState.COMPLETED,
                SessionState.FAILED,
                SessionState.DESTROYED,
            ):
                self.completed_at = time.monotonic()

            return True

    def can_transition_to(self, new_state: SessionState) -> bool:
        """Check if a transition is valid without executing it."""
        return new_state in SESSION_TRANSITIONS.get(self._state, set())

    @property
    def is_active(self) -> bool:
        return self._state in {
            SessionState.READY,
            SessionState.ACTIVE,
            SessionState.IDLE,
            SessionState.COMPLETING,
        }

    @property
    def is_terminal(self) -> bool:
        return self._state == SessionState.DESTROYED

    @property
    def is_healthy(self) -> bool:
        """Check session health: not expired, not failed, not detected."""
        if self.is_expired:
            return False
        if self._state in {
            SessionState.FAILED,
            SessionState.DETECTED,
            SessionState.DESTROYED,
            SessionState.EXPIRED,
        }:
            return False
        return True

    @property
    def is_expired(self) -> bool:
        """Check if session has exceeded its maximum duration."""
        return (time.monotonic() - self.created_at) > self.max_duration

    @property
    def is_page_limit_reached(self) -> bool:
        return self.metrics.pages_visited >= self.max_pages

    @property
    def duration(self) -> float:
        """Session duration in seconds."""
        end = self.completed_at or time.monotonic()
        return end - self.created_at

    @property
    def time_since_active(self) -> float:
        """Seconds since last activity."""
        return time.monotonic() - self.last_active

    # ── Data Store ─────────────────────────────────────────

    def set_data(self, key: str, value: Any) -> None:
        """Store arbitrary session-scoped data."""
        self._data[key] = value

    def get_data(self, key: str, default: Any = None) -> Any:
        """Retrieve session-scoped data."""
        return self._data.get(key, default)

    def update_data(self, updates: Dict[str, Any]) -> None:
        self._data.update(updates)

    # ── Cookie / Storage ───────────────────────────────────

    def set_cookie(self, name: str, value: str) -> None:
        self._cookies[name] = value

    def get_cookie(self, name: str, default: str = "") -> str:
        return self._cookies.get(name, default)

    def get_all_cookies(self) -> Dict[str, str]:
        return self._cookies.copy()

    def set_local_storage(self, key: str, value: str) -> None:
        self._local_storage[key] = value

    def get_local_storage(self, key: str, default: str = "") -> str:
        return self._local_storage.get(key, default)

    def set_session_storage(self, key: str, value: str) -> None:
        self._session_storage[key] = value

    # ── Navigation ─────────────────────────────────────────

    def record_navigation(self, url: str) -> None:
        """Record a page navigation."""
        self._nav_history.append(url)
        self.current_url = url
        self.last_active = time.monotonic()

    @property
    def nav_history(self) -> List[str]:
        return self._nav_history.copy()

    @property
    def previous_url(self) -> Optional[str]:
        if len(self._nav_history) < 2:
            return None
        return self._nav_history[-2]

    # ── Error Tracking ─────────────────────────────────────

    def record_error(
        self,
        error_type: str,
        message:    str,
        severity:   str = "error",
        metadata:   Optional[Dict[str, Any]] = None,
    ) -> None:
        """Record an error encountered during the session."""
        self._errors.append({
            "error_type": error_type,
            "message":    message,
            "severity":   severity,
            "metadata":   metadata or {},
            "timestamp":  time.monotonic(),
            "url":        self.current_url,
            "state":      self._state.name,
        })
        if severity in ("critical", "fatal"):
            self.metrics.page_errors += 1

    @property
    def error_count(self) -> int:
        return len(self._errors)

    @property
    def recent_errors(self) -> List[Dict[str, Any]]:
        return self._errors[-10:]

    @property
    def can_recover(self) -> bool:
        return self._recovery_attempts < self.max_recovery_attempts

    def increment_recovery_attempt(self) -> None:
        self._recovery_attempts += 1

    # ── Browser Handle ─────────────────────────────────────

    def attach_browser(self, driver: Any) -> None:
        """Attach a browser driver instance."""
        self._browser_driver = driver

    def detach_browser(self) -> Optional[Any]:
        """Detach and return the browser driver."""
        driver = self._browser_driver
        self._browser_driver = None
        return driver

    @property
    def has_browser(self) -> bool:
        return self._browser_driver is not None

    @property
    def browser_driver(self) -> Optional[Any]:
        return self._browser_driver

    # ── Callbacks ──────────────────────────────────────────

    def on_destroy(self, callback: Callable) -> None:
        """Register a cleanup callback for when session is destroyed."""
        self._on_destroy_callbacks.append(callback)

    async def _run_destroy_callbacks(self) -> None:
        for cb in self._on_destroy_callbacks:
            try:
                if asyncio.iscoroutinefunction(cb):
                    await cb(self)
                else:
                    cb(self)
            except Exception as exc:
                logger.error(f"[Session] Destroy callback error: {exc}")

    # ── State History ──────────────────────────────────────

    @property
    def state_history(self) -> List[Tuple[str, float]]:
        return [
            (state.name, ts)
            for state, ts in self._state_history
        ]

    def time_in_state(self, target_state: SessionState) -> float:
        """Total time spent in a given state."""
        total = 0.0
        history = self._state_history
        for i, (state, ts) in enumerate(history):
            if state == target_state:
                end_ts = history[i + 1][1] if i + 1 < len(history) else time.monotonic()
                total += end_ts - ts
        return total

    # ── Serialization ──────────────────────────────────────

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id":       self.session_id,
            "state":            self._state.name,
            "campaign_id":      self.campaign_id,
            "correlation_id":   self.correlation_id,
            "browser_id":       self.browser_id,
            "duration":         round(self.duration, 2),
            "is_healthy":       self.is_healthy,
            "is_expired":       self.is_expired,
            "error_count":      self.error_count,
            "recovery_attempts": self._recovery_attempts,
            "current_url":      self.current_url,
            "pages_visited":    self.metrics.pages_visited,
            "identity":         self.identity.to_dict(),
            "metrics":          self.metrics.to_dict(),
            "state_history":    self.state_history[-10:],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, default=str)

    def __repr__(self) -> str:
        return (
            f"Session("
            f"id={self.session_id[:8]}, "
            f"state={self._state.name}, "
            f"duration={self.duration:.1f}s, "
            f"pages={self.metrics.pages_visited})"
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Session Pool
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class SessionPool:
    """
    High-performance session container with indexed lookups.
    Supports querying by state, campaign, proxy, and identity.
    """

    def __init__(self, max_sessions: int = 200):
        self._sessions:         Dict[str, Session]              = {}
        self._by_state:         Dict[SessionState, Set[str]]    = defaultdict(set)
        self._by_campaign:      Dict[str, Set[str]]             = defaultdict(set)
        self._by_proxy:         Dict[str, Set[str]]             = defaultdict(set)
        self._by_browser:       Dict[str, Set[str]]             = defaultdict(set)
        self._by_traffic_type:  Dict[TrafficType, Set[str]]     = defaultdict(set)
        self._max_sessions      = max_sessions
        self._lock              = asyncio.Lock()
        self._total_created:    int = 0
        self._total_completed:  int = 0
        self._total_failed:     int = 0

    async def add(self, session: Session) -> bool:
        """Add session to pool. Returns False if at capacity."""
        async with self._lock:
            if len(self._sessions) >= self._max_sessions:
                return False

            sid = session.session_id
            self._sessions[sid] = session
            self._by_state[session.state].add(sid)

            if session.campaign_id:
                self._by_campaign[session.campaign_id].add(sid)
            if session.identity.proxy_id:
                self._by_proxy[session.identity.proxy_id].add(sid)
            if session.browser_id:
                self._by_browser[session.browser_id].add(sid)

            self._by_traffic_type[session.identity.traffic_type].add(sid)
            self._total_created += 1
            return True

    async def remove(self, session_id: str) -> Optional[Session]:
        """Remove and return a session from all indexes."""
        async with self._lock:
            session = self._sessions.pop(session_id, None)
            if session is None:
                return None

            # Clean up all indexes
            for state_set in self._by_state.values():
                state_set.discard(session_id)
            for camp_set in self._by_campaign.values():
                camp_set.discard(session_id)
            for proxy_set in self._by_proxy.values():
                proxy_set.discard(session_id)
            for browser_set in self._by_browser.values():
                browser_set.discard(session_id)
            for type_set in self._by_traffic_type.values():
                type_set.discard(session_id)

            if session.state == SessionState.COMPLETED:
                self._total_completed += 1
            elif session.state in {SessionState.FAILED, SessionState.DETECTED}:
                self._total_failed += 1

            return session

    async def update_state_index(
        self,
        session_id: str,
        old_state:  SessionState,
        new_state:  SessionState,
    ) -> None:
        """Update state index when a session changes state."""
        async with self._lock:
            self._by_state[old_state].discard(session_id)
            self._by_state[new_state].add(session_id)

    def get(self, session_id: str) -> Optional[Session]:
        return self._sessions.get(session_id)

    def get_by_state(self, state: SessionState) -> List[Session]:
        ids = self._by_state.get(state, set()).copy()
        return [self._sessions[sid] for sid in ids if sid in self._sessions]

    def get_by_campaign(self, campaign_id: str) -> List[Session]:
        ids = self._by_campaign.get(campaign_id, set()).copy()
        return [self._sessions[sid] for sid in ids if sid in self._sessions]

    def get_by_proxy(self, proxy_id: str) -> List[Session]:
        ids = self._by_proxy.get(proxy_id, set()).copy()
        return [self._sessions[sid] for sid in ids if sid in self._sessions]

    def get_by_browser(self, browser_id: str) -> List[Session]:
        ids = self._by_browser.get(browser_id, set()).copy()
        return [self._sessions[sid] for sid in ids if sid in self._sessions]

    def get_by_traffic_type(self, traffic_type: TrafficType) -> List[Session]:
        ids = self._by_traffic_type.get(traffic_type, set()).copy()
        return [self._sessions[sid] for sid in ids if sid in self._sessions]

    def get_active(self) -> List[Session]:
        """Get all sessions in an active-equivalent state."""
        active_states = {
            SessionState.READY,
            SessionState.ACTIVE,
            SessionState.IDLE,
            SessionState.COMPLETING,
        }
        result = []
        for state in active_states:
            result.extend(self.get_by_state(state))
        return result

    def get_all(self) -> List[Session]:
        return list(self._sessions.values())

    def query(
        self,
        state:        Optional[SessionState]  = None,
        traffic_type: Optional[TrafficType]   = None,
        campaign_id:  Optional[str]           = None,
        healthy_only: bool                    = False,
        limit:        Optional[int]           = None,
    ) -> List[Session]:
        """Flexible session query with multiple filters."""
        results = list(self._sessions.values())

        if state is not None:
            results = [s for s in results if s.state == state]
        if traffic_type is not None:
            results = [s for s in results if s.identity.traffic_type == traffic_type]
        if campaign_id is not None:
            results = [s for s in results if s.campaign_id == campaign_id]
        if healthy_only:
            results = [s for s in results if s.is_healthy]
        if limit is not None:
            results = results[:limit]

        return results

    @property
    def size(self) -> int:
        return len(self._sessions)

    @property
    def is_full(self) -> bool:
        return len(self._sessions) >= self._max_sessions

    @property
    def capacity_pct(self) -> float:
        return len(self._sessions) / self._max_sessions

    def state_summary(self) -> Dict[str, int]:
        return {
            state.name: len(ids)
            for state, ids in self._by_state.items()
            if ids
        }

    def stats(self) -> Dict[str, Any]:
        sessions = list(self._sessions.values())
        durations = [s.duration for s in sessions if s.started_at]
        return {
            "total":            len(sessions),
            "max":              self._max_sessions,
            "capacity_pct":     round(self.capacity_pct * 100, 1),
            "by_state":         self.state_summary(),
            "total_created":    self._total_created,
            "total_completed":  self._total_completed,
            "total_failed":     self._total_failed,
            "avg_duration":     round(sum(durations) / len(durations), 2) if durations else 0,
        }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Session Identity Factory
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class SessionIdentityFactory:
    """
    Builds coherent SessionIdentity objects.
    Ensures all identity components are consistent with each other.
    (OS ↔ UA, device type ↔ viewport, locale ↔ timezone, etc.)
    """

    # Viewport presets per device type
    DESKTOP_VIEWPORTS = [
        (1920, 1080), (1366, 768), (1536, 864),
        (1440, 900),  (1280, 720), (1600, 900),
        (2560, 1440), (1280, 800), (1024, 768),
    ]
    DESKTOP_VIEWPORT_WEIGHTS = [25, 20, 15, 12, 10, 8, 5, 3, 2]

    MOBILE_VIEWPORTS = [
        (390, 844), (414, 896), (375, 667),
        (360, 780), (393, 851), (412, 915),
    ]

    TABLET_VIEWPORTS = [
        (768, 1024), (820, 1180), (800, 1280),
        (1024, 1366), (810, 1080),
    ]

    # OS/Platform combos
    DESKTOP_PLATFORMS = [
        ("Windows",  "10.0", "Win32"),
        ("Windows",  "11.0", "Win32"),
        ("Macintosh", "10.15.7", "MacIntel"),
        ("Macintosh", "13.0",   "MacIntel"),
        ("Linux",    "x86_64",  "Linux x86_64"),
    ]
    DESKTOP_PLATFORM_WEIGHTS = [35, 20, 18, 12, 15]

    MOBILE_PLATFORMS = [
        ("Android", "13",  "Linux armv8l"),
        ("Android", "12",  "Linux armv8l"),
        ("iPhone",  "16.0", "iPhone"),
        ("iPhone",  "15.0", "iPhone"),
    ]
    MOBILE_PLATFORM_WEIGHTS = [30, 25, 30, 15]

    # GEO data
    GEO_PROFILES = {
        "US": {"timezone": "America/New_York",   "lang": "en-US", "locale": "en-US", "country": "United States"},
        "GB": {"timezone": "Europe/London",      "lang": "en-GB", "locale": "en-GB", "country": "United Kingdom"},
        "CA": {"timezone": "America/Toronto",    "lang": "en-CA", "locale": "en-CA", "country": "Canada"},
        "AU": {"timezone": "Australia/Sydney",   "lang": "en-AU", "locale": "en-AU", "country": "Australia"},
        "DE": {"timezone": "Europe/Berlin",      "lang": "de-DE", "locale": "de-DE", "country": "Germany"},
        "FR": {"timezone": "Europe/Paris",       "lang": "fr-FR", "locale": "fr-FR", "country": "France"},
        "JP": {"timezone": "Asia/Tokyo",         "lang": "ja-JP", "locale": "ja-JP", "country": "Japan"},
        "BR": {"timezone": "America/Sao_Paulo",  "lang": "pt-BR", "locale": "pt-BR", "country": "Brazil"},
        "IN": {"timezone": "Asia/Kolkata",       "lang": "en-IN", "locale": "en-IN", "country": "India"},
        "SG": {"timezone": "Asia/Singapore",     "lang": "en-SG", "locale": "en-SG", "country": "Singapore"},
    }

    # WebGL renderer profiles per platform
    WEBGL_PROFILES = {
        "Windows": [
            ("NVIDIA Corporation", "ANGLE (NVIDIA, NVIDIA GeForce GTX 1080 Direct3D11 vs_5_0 ps_5_0, D3D11-27.21.14.6589)"),
            ("NVIDIA Corporation", "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11-31.0.15.2699)"),
            ("Intel Inc.",         "ANGLE (Intel, Intel(R) UHD Graphics 620 Direct3D11 vs_5_0 ps_5_0, D3D11-27.20.100.9316)"),
            ("ATI Technologies",   "ANGLE (AMD, Radeon RX 580 Series Direct3D11 vs_5_0 ps_5_0, D3D11-27.20.22028.10)"),
        ],
        "Macintosh": [
            ("Apple Inc.", "Apple M1"),
            ("Apple Inc.", "Apple M2"),
            ("Intel Inc.", "Intel Iris Pro OpenGL Engine"),
        ],
        "Linux": [
            ("Mesa/X.org", "Mesa DRI Intel(R) UHD Graphics 620 (KBL GT2)"),
            ("NVIDIA Corporation", "GeForce GTX 1050/PCIe/SSE2"),
        ],
        "Android": [
            ("Qualcomm", "Adreno (TM) 650"),
            ("ARM",      "Mali-G77 MP11"),
        ],
        "iPhone": [
            ("Apple Inc.", "Apple GPU"),
        ],
    }

    def __init__(
        self,
        config:         ConfigManager,
        user_agent_db:  Optional[Dict[str, Any]] = None,
        geo_weights:    Optional[Dict[str, float]] = None,
    ):
        self._config        = config
        self._ua_db         = user_agent_db or {}
        self._geo_weights   = geo_weights or {
            "US": 0.40, "GB": 0.15, "CA": 0.12,
            "AU": 0.10, "DE": 0.08, "FR": 0.05,
            "JP": 0.04, "BR": 0.03, "IN": 0.02, "SG": 0.01,
        }

    def build(
        self,
        session_id:     Optional[str]           = None,
        proxy_info:     Optional[Dict[str, Any]] = None,
        traffic_type:   Optional[TrafficType]   = None,
        device_type:    Optional[DeviceType]    = None,
        country_code:   Optional[str]           = None,
        entry_url:      str                     = "",
        referrer:       str                     = "",
        search_keyword: Optional[str]           = None,
        search_engine:  Optional[str]           = None,
        fingerprint_id: Optional[str]           = None,
    ) -> SessionIdentity:
        """
        Build a complete, internally consistent SessionIdentity.
        All components are chosen to be mutually consistent.
        """
        sid = session_id or str(uuid.uuid4())
        fp_id = fingerprint_id or str(uuid.uuid4())[:16]

        # Choose device type
        if device_type is None:
            dist = self._config.get("traffic.device_distribution",
                                    {"desktop": 0.65, "mobile": 0.30, "tablet": 0.05})
            device_type = self._weighted_choice({
                DeviceType.DESKTOP: dist.get("desktop", 0.65),
                DeviceType.MOBILE:  dist.get("mobile",  0.30),
                DeviceType.TABLET:  dist.get("tablet",  0.05),
            })

        # Choose country / geo profile
        if country_code is None:
            country_code = self._weighted_choice(self._geo_weights)
        geo = self.GEO_PROFILES.get(country_code, self.GEO_PROFILES["US"])

        # Override with proxy geo if available
        if proxy_info:
            geo_country = proxy_info.get("country_code", country_code)
            if geo_country in self.GEO_PROFILES:
                geo = self.GEO_PROFILES[geo_country]

        # Choose platform based on device type
        platform_info = self._choose_platform(device_type)
        os_name, os_version, os_platform = platform_info

        # Choose viewport
        viewport_w, viewport_h = self._choose_viewport(device_type)

        # Build user agent
        ua, browser_version = self._build_user_agent(device_type, os_name, os_version)

        # Choose WebGL profile
        webgl_vendor, webgl_renderer = self._choose_webgl(os_name)

        # Traffic type
        if traffic_type is None:
            traffic_type = self._choose_traffic_type()

        # Behavioral params
        typing_wpm = random.randint(
            self._config.get("behavior.typing_wpm_min", 35),
            self._config.get("behavior.typing_wpm_max", 95),
        )

        # Build languages list (primary + secondary)
        primary_lang = geo["lang"]
        langs = [primary_lang]
        if primary_lang.startswith("en"):
            langs.append("en")
        elif not primary_lang.startswith("en"):
            langs.extend([primary_lang, "en-US", "en"])

        # Proxy info
        proxy_id      = proxy_info.get("proxy_id")      if proxy_info else None
        proxy_address = proxy_info.get("address")        if proxy_info else None
        proxy_protocol = proxy_info.get("protocol")     if proxy_info else None
        proxy_country = proxy_info.get("country", geo.get("country", "Unknown")) if proxy_info else geo.get("country", "Unknown")
        proxy_city    = proxy_info.get("city", "Unknown") if proxy_info else "Unknown"
        proxy_isp     = proxy_info.get("isp", "Unknown")  if proxy_info else "Unknown"
        proxy_asn     = proxy_info.get("asn", "")         if proxy_info else ""

        identity = SessionIdentity(
            session_id          = sid,
            fingerprint_id      = fp_id,
            proxy_id            = proxy_id,
            user_agent          = ua,
            browser_version     = browser_version,
            os_platform         = os_name,
            os_version          = os_version,
            device_type         = device_type,
            viewport_width      = viewport_w,
            viewport_height     = viewport_h,
            color_depth         = random.choice([24, 30, 32]),
            pixel_ratio         = random.choice([1.0, 1.25, 1.5, 2.0]),
            proxy_address       = proxy_address,
            proxy_protocol      = proxy_protocol,
            proxy_country       = proxy_country,
            proxy_city          = proxy_city,
            proxy_isp           = proxy_isp,
            proxy_asn           = proxy_asn,
            language            = primary_lang,
            languages           = langs,
            timezone            = geo["timezone"],
            locale              = geo["locale"],
            canvas_noise_seed   = random.randint(1, 2 ** 31 - 1),
            webgl_renderer      = webgl_renderer,
            webgl_vendor        = webgl_vendor,
            audio_noise_seed    = random.randint(1, 2 ** 31 - 1),
            tls_fingerprint_id  = self._choose_tls_profile(browser_version),
            ja3_hash            = self._generate_ja3(browser_version),
            traffic_type        = traffic_type,
            referrer            = referrer,
            entry_url           = entry_url,
            search_keyword      = search_keyword,
            search_engine       = search_engine,
            typing_wpm          = typing_wpm,
            mouse_speed         = random.uniform(300, 1200),
            scroll_speed        = random.choice(["slow", "normal", "fast"]),
            read_speed          = random.uniform(2.5, 5.0),
            idle_frequency      = random.uniform(0.02, 0.15),
        )

        logger.debug(
            f"[IdentityFactory] Built: {sid[:8]} | "
            f"device={device_type.value} | country={country_code} | "
            f"ua={ua[:60]!r}"
        )
        return identity

    # ── Internal helpers ───────────────────────────────────

    def _choose_platform(
        self, device_type: DeviceType
    ) -> Tuple[str, str, str]:
        if device_type == DeviceType.DESKTOP:
            return self._weighted_choice_list(
                self.DESKTOP_PLATFORMS,
                self.DESKTOP_PLATFORM_WEIGHTS,
            )
        return self._weighted_choice_list(
            self.MOBILE_PLATFORMS,
            self.MOBILE_PLATFORM_WEIGHTS,
        )

    def _choose_viewport(self, device_type: DeviceType) -> Tuple[int, int]:
        if device_type == DeviceType.DESKTOP:
            return self._weighted_choice_list(
                self.DESKTOP_VIEWPORTS,
                self.DESKTOP_VIEWPORT_WEIGHTS,
            )
        elif device_type == DeviceType.MOBILE:
            return random.choice(self.MOBILE_VIEWPORTS)
        return random.choice(self.TABLET_VIEWPORTS)

    def _build_user_agent(
        self,
        device_type: DeviceType,
        os_name:     str,
        os_version:  str,
    ) -> Tuple[str, str]:
        """Build a realistic user agent string."""
        chrome_versions = [
            "120.0.6099.130", "121.0.6167.85", "122.0.6261.69",
            "123.0.6312.122", "124.0.6367.82", "125.0.6422.142",
        ]
        version = random.choice(chrome_versions)
        major = version.split(".")[0]

        if device_type == DeviceType.DESKTOP:
            if os_name == "Windows":
                ua = (
                    f"Mozilla/5.0 (Windows NT {os_version}; Win64; x64) "
                    f"AppleWebKit/537.36 (KHTML, like Gecko) "
                    f"Chrome/{version} Safari/537.36"
                )
            elif os_name == "Macintosh":
                mac_ver = os_version.replace(".", "_")
                ua = (
                    f"Mozilla/5.0 (Macintosh; Intel Mac OS X {mac_ver}) "
                    f"AppleWebKit/537.36 (KHTML, like Gecko) "
                    f"Chrome/{version} Safari/537.36"
                )
            else:  # Linux
                ua = (
                    f"Mozilla/5.0 (X11; Linux x86_64) "
                    f"AppleWebKit/537.36 (KHTML, like Gecko) "
                    f"Chrome/{version} Safari/537.36"
                )
        elif os_name == "iPhone":
            webkit_ver = "605.1.15"
            ua = (
                f"Mozilla/5.0 (iPhone; CPU iPhone OS "
                f"{os_version.replace('.', '_')} like Mac OS X) "
                f"AppleWebKit/{webkit_ver} (KHTML, like Gecko) "
                f"CriOS/{version} Mobile/15E148 Safari/{webkit_ver}"
            )
        else:  # Android
            ua = (
                f"Mozilla/5.0 (Linux; Android {os_version}; Pixel 6) "
                f"AppleWebKit/537.36 (KHTML, like Gecko) "
                f"Chrome/{version} Mobile Safari/537.36"
            )

        return ua, version

    def _choose_webgl(self, os_name: str) -> Tuple[str, str]:
        profiles = self.WEBGL_PROFILES.get(os_name, self.WEBGL_PROFILES["Windows"])
        vendor, renderer = random.choice(profiles)
        return vendor, renderer

    def _choose_traffic_type(self) -> TrafficType:
        cfg = self._config
        weights = {
            TrafficType.ORGANIC:  cfg.get("traffic.organic_ratio",  0.60),
            TrafficType.SOCIAL:   cfg.get("traffic.social_ratio",   0.15),
            TrafficType.DIRECT:   cfg.get("traffic.direct_ratio",   0.15),
            TrafficType.REFERRAL: cfg.get("traffic.referral_ratio", 0.10),
        }
        return self._weighted_choice(weights)

    def _choose_tls_profile(self, chrome_version: str) -> str:
        """Choose a TLS fingerprint profile ID compatible with this Chrome version."""
        major = int(chrome_version.split(".")[0])
        if major >= 120:
            profiles = ["chrome120_tls", "chrome121_tls", "chrome122_tls"]
        elif major >= 110:
            profiles = ["chrome110_tls", "chrome115_tls"]
        else:
            profiles = ["chrome100_tls"]
        return random.choice(profiles)

    def _generate_ja3(self, chrome_version: str) -> str:
        """Generate a realistic JA3 hash for the given Chrome version."""
        # Real Chrome JA3 hashes for different versions
        ja3_hashes = {
            "120": "cd08e31494f9531f560d64c695473da9",
            "121": "8e0d6e2e0f92cbabc60d2b23a01af01a",
            "122": "54328bd36c14bd82ddaa0c04b25ed9ad",
            "123": "66918128f1b9b03303d77c6f2eefd128",
            "124": "b32309a26951912be7dba376398abc3b",
            "125": "2e0e57bc5a4ff08bafdc64f0882c99c8",
        }
        major = chrome_version.split(".")[0]
        return ja3_hashes.get(major, ja3_hashes["125"])

    @staticmethod
    def _weighted_choice(weights: Dict[Any, float]) -> Any:
        """Select a key from dict weighted by values."""
        items = list(weights.keys())
        wts   = list(weights.values())
        total = sum(wts)
        normalized = [w / total for w in wts]
        r = random.random()
        cumulative = 0.0
        for item, weight in zip(items, normalized):
            cumulative += weight
            if r <= cumulative:
                return item
        return items[-1]

    @staticmethod
    def _weighted_choice_list(items: List[Any], weights: List[float]) -> Any:
        total = sum(weights)
        r = random.random() * total
        cumulative = 0.0
        for item, weight in zip(items, weights):
            cumulative += weight
            if r <= cumulative:
                return item
        return items[-1]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Session Manager
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class SessionManager:
    """
    Jubra Traffic Pro - Advanced Session Manager

    Responsibilities:
    ─────────────────────────────────────────────────────
    • Session creation with full identity generation
    • Session lifecycle management (state machine enforcement)
    • Concurrent session limiting and queue management
    • Session health monitoring and auto-expiry
    • Session recovery after failures
    • Resource cleanup (browser, proxy release)
    • Metrics aggregation across all sessions
    • EventBus integration for real-time monitoring
    • Session recycling for efficiency
    • Campaign-level session tracking
    • Rate limiting and scheduling support
    """

    def __init__(
        self,
        config:             ConfigManager,
        event_bus:          Optional[EventBus]   = None,
        max_sessions:       int                  = 50,
        session_ttl:        float                = 600.0,
        health_check_interval: float             = 10.0,
        enable_recycling:   bool                 = True,
        max_recovery_attempts: int               = 3,
    ):
        self._config                = config
        self._event_bus             = event_bus or get_event_bus()
        self._max_sessions          = max_sessions
        self._session_ttl           = session_ttl
        self._health_check_interval = health_check_interval
        self._enable_recycling      = enable_recycling
        self._max_recovery_attempts = max_recovery_attempts

        # Core components
        self._pool          = SessionPool(max_sessions)
        self._identity_factory = SessionIdentityFactory(config)

        # Waiting queue for when pool is full
        self._wait_queue:   asyncio.Queue = asyncio.Queue()

        # Background tasks
        self._health_task:  Optional[asyncio.Task] = None
        self._running:      bool = False

        # Aggregate metrics
        self._total_sessions_created:   int   = 0
        self._total_sessions_completed: int   = 0
        self._total_sessions_failed:    int   = 0
        self._total_sessions_detected:  int   = 0
        self._total_sessions_recovered: int   = 0
        self._session_durations:        deque = deque(maxlen=1000)
        self._success_durations:        deque = deque(maxlen=1000)
        self._hourly_counts:            Dict[int, int] = defaultdict(int)

        # Rate limiting
        self._rate_limiter_lock = asyncio.Lock()
        self._last_creation_time: float = 0.0
        self._creation_count_window: deque = deque(maxlen=3600)

        # Lifecycle callbacks
        self._pre_create_hooks:    List[Callable] = []
        self._post_create_hooks:   List[Callable] = []
        self._pre_destroy_hooks:   List[Callable] = []
        self._post_destroy_hooks:  List[Callable] = []

        logger.info(
            f"[SessionManager] Initialized: "
            f"max={max_sessions}, ttl={session_ttl}s, "
            f"recycle={enable_recycling}"
        )

    # ── Lifecycle ──────────────────────────────────────────

    async def start(self) -> None:
        """Start the session manager background tasks."""
        self._running = True
        self._health_task = asyncio.create_task(
            self._health_monitor_loop(),
            name="SessionManager-HealthMonitor",
        )
        logger.info("[SessionManager] Started")

    async def stop(self) -> None:
        """Stop the session manager and destroy all sessions."""
        self._running = False
        if self._health_task:
            self._health_task.cancel()
            try:
                await self._health_task
            except asyncio.CancelledError:
                pass

        # Destroy all active sessions
        all_sessions = self._pool.get_all()
        destroy_tasks = [
            self.destroy_session(s.session_id, reason="manager_shutdown")
            for s in all_sessions
        ]
        if destroy_tasks:
            await asyncio.gather(*destroy_tasks, return_exceptions=True)

        logger.info("[SessionManager] Stopped")

    # ── Session Creation ───────────────────────────────────

    async def create_session(
        self,
        proxy_info:     Optional[Dict[str, Any]] = None,
        traffic_type:   Optional[TrafficType]    = None,
        device_type:    Optional[DeviceType]     = None,
        country_code:   Optional[str]            = None,
        entry_url:      str                      = "",
        referrer:       str                      = "",
        search_keyword: Optional[str]            = None,
        search_engine:  Optional[str]            = None,
        campaign_id:    Optional[str]            = None,
        max_duration:   Optional[float]          = None,
        max_pages:      Optional[int]            = None,
        correlation_id: Optional[str]            = None,
        wait_timeout:   float                    = 60.0,
    ) -> Session:
        """
        Create a new session with full identity generation.

        If pool is at capacity, waits up to wait_timeout seconds
        for a slot to become available.
        """
        # Check capacity with wait
        if self._pool.is_full:
            logger.debug(
                f"[SessionManager] Pool full ({self._pool.size}/{self._max_sessions}), "
                f"waiting up to {wait_timeout}s"
            )
            try:
                await asyncio.wait_for(
                    self._wait_for_slot(),
                    timeout=wait_timeout,
                )
            except asyncio.TimeoutError:
                raise SessionLimitError(
                    current=self._pool.size,
                    maximum=self._max_sessions,
                    context=ErrorContext(
                        module="SessionManager",
                        operation="create_session",
                    ),
                )

        # Run pre-create hooks
        for hook in self._pre_create_hooks:
            try:
                if asyncio.iscoroutinefunction(hook):
                    await hook(traffic_type, device_type)
                else:
                    hook(traffic_type, device_type)
            except Exception as exc:
                logger.error(f"[SessionManager] Pre-create hook error: {exc}")

        try:
            # Build identity
            identity = self._identity_factory.build(
                proxy_info      = proxy_info,
                traffic_type    = traffic_type,
                device_type     = device_type,
                country_code    = country_code,
                entry_url       = entry_url,
                referrer        = referrer,
                search_keyword  = search_keyword,
                search_engine   = search_engine,
            )

            # Create session object
            session = Session(
                identity        = identity,
                campaign_id     = campaign_id,
                max_duration    = max_duration or self._session_ttl,
                max_pages       = max_pages or self._config.get(
                    "traffic.pages_per_session.max", 20
                ),
                correlation_id  = correlation_id,
            )
            session.max_recovery_attempts = self._max_recovery_attempts

            # Add to pool
            if not await self._pool.add(session):
                raise SessionCreationError(
                    reason="Pool add failed unexpectedly",
                    context=ErrorContext(
                        module="SessionManager",
                        operation="create_session",
                        session_id=session.session_id,
                    ),
                )

            # Transition to READY
            await session.transition_to(SessionState.READY)

            # Update counters
            self._total_sessions_created += 1
            self._creation_count_window.append(time.monotonic())
            hour_key = int(time.time() // 3600)
            self._hourly_counts[hour_key] += 1

            # Emit event
            await self._event_bus.publish_simple(
                EventCategory.SESSION_CREATED,
                {
                    "session_id":    session.session_id,
                    "traffic_type":  identity.traffic_type.value,
                    "device_type":   identity.device_type.value,
                    "country":       identity.proxy_country,
                    "campaign_id":   campaign_id,
                    "pool_size":     self._pool.size,
                },
                priority=EventPriority.NORMAL,
                session_id=session.session_id,
                correlation_id=correlation_id,
            )

            # Run post-create hooks
            for hook in self._post_create_hooks:
                try:
                    if asyncio.iscoroutinefunction(hook):
                        await hook(session)
                    else:
                        hook(session)
                except Exception as exc:
                    logger.error(f"[SessionManager] Post-create hook error: {exc}")

            logger.debug(
                f"[SessionManager] Created: {session.session_id[:8]} | "
                f"pool: {self._pool.size}/{self._max_sessions}"
            )
            return session

        except (SessionLimitError, SessionCreationError):
            raise
        except Exception as exc:
            raise SessionCreationError(
                reason=str(exc),
                context=ErrorContext(
                    module="SessionManager",
                    operation="create_session",
                ),
            ) from exc

    # ── Session Lifecycle ──────────────────────────────────

    async def activate_session(self, session_id: str) -> Session:
        """Mark session as actively running traffic."""
        session = self._get_or_raise(session_id)
        old_state = session.state
        await session.transition_to(SessionState.ACTIVE)
        await self._pool.update_state_index(session_id, old_state, SessionState.ACTIVE)
        session.started_at = session.started_at or time.monotonic()

        await self._event_bus.publish_simple(
            EventCategory.SESSION_STARTED,
            {
                "session_id":   session_id,
                "traffic_type": session.identity.traffic_type.value,
                "entry_url":    session.identity.entry_url,
            },
            session_id=session_id,
            correlation_id=session.correlation_id,
        )
        return session

    async def idle_session(self, session_id: str) -> Session:
        """Mark session as temporarily idle (human reading/thinking)."""
        session = self._get_or_raise(session_id)
        if session.can_transition_to(SessionState.IDLE):
            old_state = session.state
            await session.transition_to(SessionState.IDLE)
            await self._pool.update_state_index(session_id, old_state, SessionState.IDLE)
        return session

    async def resume_session(self, session_id: str) -> Session:
        """Resume session from idle state."""
        session = self._get_or_raise(session_id)
        if session.can_transition_to(SessionState.ACTIVE):
            old_state = session.state
            await session.transition_to(SessionState.ACTIVE)
            await self._pool.update_state_index(session_id, old_state, SessionState.ACTIVE)
        return session

    async def complete_session(
        self,
        session_id: str,
        success:    bool = True,
    ) -> Optional[Session]:
        """Mark session as completing and transition to completed/failed."""
        session = self._get_or_raise(session_id)
        if not session:
            return None

        # Transition to completing
        if session.can_transition_to(SessionState.COMPLETING):
            old_state = session.state
            await session.transition_to(SessionState.COMPLETING)
            await self._pool.update_state_index(
                session_id, old_state, SessionState.COMPLETING
            )

        # Transition to completed or failed
        final_state = SessionState.COMPLETED if success else SessionState.FAILED
        if session.can_transition_to(final_state):
            old_state = session.state
            await session.transition_to(final_state)
            await self._pool.update_state_index(
                session_id, old_state, final_state
            )

        # Record duration
        dur = session.duration
        self._session_durations.append(dur)
        if success:
            self._success_durations.append(dur)
            self._total_sessions_completed += 1
        else:
            self._total_sessions_failed += 1

        # Emit event
        event_cat = (
            EventCategory.SESSION_COMPLETED
            if success else EventCategory.SESSION_FAILED
        )
        await self._event_bus.publish_simple(
            event_cat,
            {
                "session_id":       session_id,
                "duration":         round(dur, 2),
                "pages_visited":    session.metrics.pages_visited,
                "engagement_score": session.metrics.engagement_score,
                "goals_reached":    session.metrics.goals_reached,
                "bounce":           session.metrics.bounce,
            },
            session_id=session_id,
            correlation_id=session.correlation_id,
        )

        # Recycle or destroy
        if success and self._enable_recycling:
            await self._try_recycle(session)
        else:
            await self.destroy_session(session_id)

        return session

    async def fail_session(
        self,
        session_id: str,
        reason:     str = "",
        detected:   bool = False,
    ) -> Optional[Session]:
        """Mark session as failed (optionally due to bot detection)."""
        session = self._get_or_raise(session_id)
        if not session:
            return None

        new_state = SessionState.DETECTED if detected else SessionState.FAILED
        if session.can_transition_to(new_state):
            old_state = session.state
            await session.transition_to(new_state)
            await self._pool.update_state_index(session_id, old_state, new_state)

        session.record_error(
            "session_failure",
            reason,
            severity="error" if not detected else "critical",
        )

        if detected:
            self._total_sessions_detected += 1
            await self._event_bus.publish_simple(
                EventCategory.DETECTION_BOT_DETECTED,
                {
                    "session_id":     session_id,
                    "reason":         reason,
                    "proxy_id":       session.identity.proxy_id,
                    "fingerprint_id": session.identity.fingerprint_id,
                    "url":            session.current_url,
                },
                priority=EventPriority.HIGH,
                session_id=session_id,
            )

        # Attempt recovery if possible
        if session.can_recover and not detected:
            return await self._attempt_recovery(session, reason)

        await self.destroy_session(session_id)
        return session

    async def destroy_session(
        self,
        session_id: str,
        reason:     str = "",
    ) -> None:
        """
        Fully destroy a session and release all resources.
        """
        session = self._pool.get(session_id)
        if not session:
            return

        # Run pre-destroy hooks
        for hook in self._pre_destroy_hooks:
            try:
                if asyncio.iscoroutinefunction(hook):
                    await hook(session)
                else:
                    hook(session)
            except Exception as exc:
                logger.error(f"[SessionManager] Pre-destroy hook: {exc}")

        # Run session's own destroy callbacks
        await session._run_destroy_callbacks()

        # Detach browser (caller must return it to browser pool)
        if session.has_browser:
            driver = session.detach_browser()
            logger.debug(
                f"[SessionManager] Detached browser from {session_id[:8]}"
            )

        # Transition to destroyed
        try:
            if session.can_transition_to(SessionState.DESTROYED):
                old = session.state
                await session.transition_to(SessionState.DESTROYED)
                await self._pool.update_state_index(
                    session_id, old, SessionState.DESTROYED
                )
        except Exception:
            pass

        # Remove from pool
        removed = await self._pool.remove(session_id)

        # Signal waiting creators
        if not self._wait_queue.empty():
            try:
                self._wait_queue.get_nowait()
            except asyncio.QueueEmpty:
                pass

        # Run post-destroy hooks
        for hook in self._post_destroy_hooks:
            try:
                if asyncio.iscoroutinefunction(hook):
                    await hook(session_id, reason)
                else:
                    hook(session_id, reason)
            except Exception as exc:
                logger.error(f"[SessionManager] Post-destroy hook: {exc}")

        logger.debug(
            f"[SessionManager] Destroyed: {session_id[:8]} | "
            f"reason={reason!r} | pool: {self._pool.size}/{self._max_sessions}"
        )

    # ── Context Manager ────────────────────────────────────

    @asynccontextmanager
    async def session_context(
        self,
        **create_kwargs,
    ) -> AsyncIterator[Session]:
        """
        Async context manager for automatic session lifecycle management.

        Usage:
            async with manager.session_context(
                traffic_type=TrafficType.ORGANIC,
                entry_url="https://example.com",
            ) as session:
                # Do traffic simulation
                ...
            # Session is auto-completed/destroyed on exit
        """
        session = await self.create_session(**create_kwargs)
        success = False
        try:
            await self.activate_session(session.session_id)
            yield session
            success = True
        except Exception as exc:
            session.record_error("context_error", str(exc), severity="error")
            raise
        finally:
            if not session.is_terminal:
                await self.complete_session(session.session_id, success=success)

    # ── Hook Registration ──────────────────────────────────

    def add_pre_create_hook(self, hook: Callable) -> None:
        self._pre_create_hooks.append(hook)

    def add_post_create_hook(self, hook: Callable) -> None:
        self._post_create_hooks.append(hook)

    def add_pre_destroy_hook(self, hook: Callable) -> None:
        self._pre_destroy_hooks.append(hook)

    def add_post_destroy_hook(self, hook: Callable) -> None:
        self._post_destroy_hooks.append(hook)

    # ── Query ──────────────────────────────────────────────

    def get_session(self, session_id: str) -> Optional[Session]:
        return self._pool.get(session_id)

    def get_active_sessions(self) -> List[Session]:
        return self._pool.get_active()

    def get_sessions_by_campaign(self, campaign_id: str) -> List[Session]:
        return self._pool.get_by_campaign(campaign_id)

    def query_sessions(self, **kwargs) -> List[Session]:
        return self._pool.query(**kwargs)

    @property
    def active_count(self) -> int:
        return len(self._pool.get_active())

    @property
    def pool_size(self) -> int:
        return self._pool.size

    @property
    def is_at_capacity(self) -> bool:
        return self._pool.is_full

    # ── Metrics ────────────────────────────────────────────

    @property
    def success_rate(self) -> float:
        total = self._total_sessions_completed + self._total_sessions_failed
        if total == 0:
            return 1.0
        return self._total_sessions_completed / total

    @property
    def detection_rate(self) -> float:
        total = self._total_sessions_created
        if total == 0:
            return 0.0
        return self._total_sessions_detected / total

    @property
    def avg_session_duration(self) -> float:
        if not self._session_durations:
            return 0.0
        return sum(self._session_durations) / len(self._session_durations)

    @property
    def sessions_per_hour(self) -> float:
        now = time.monotonic()
        window = 3600.0
        recent = [t for t in self._creation_count_window if now - t <= window]
        return len(recent)

    def get_full_metrics(self) -> Dict[str, Any]:
        return {
            "pool":                 self._pool.stats(),
            "total_created":        self._total_sessions_created,
            "total_completed":      self._total_sessions_completed,
            "total_failed":         self._total_sessions_failed,
            "total_detected":       self._total_sessions_detected,
            "total_recovered":      self._total_sessions_recovered,
            "success_rate":         round(self.success_rate, 4),
            "detection_rate":       round(self.detection_rate, 4),
            "avg_duration_s":       round(self.avg_session_duration, 2),
            "sessions_per_hour":    round(self.sessions_per_hour, 1),
            "pool_state_summary":   self._pool.state_summary(),
        }

    # ── Internal ───────────────────────────────────────────

    def _get_or_raise(self, session_id: str) -> Session:
        session = self._pool.get(session_id)
        if session is None:
            raise SessionError(
                f"Session not found: {session_id}",
                context=ErrorContext(
                    module="SessionManager",
                    session_id=session_id,
                ),
            )
        return session

    async def _wait_for_slot(self) -> None:
        """Wait until a pool slot becomes available."""
        await self._wait_queue.put(None)
        while self._pool.is_full:
            await asyncio.sleep(0.5)

    async def _try_recycle(self, session: Session) -> None:
        """Attempt to recycle a completed session."""
        try:
            old = session.state
            await session.transition_to(SessionState.RECYCLING)
            await self._pool.update_state_index(
                session.session_id, old, SessionState.RECYCLING
            )

            # Clear session state for reuse
            session.metrics = SessionMetrics()
            session._cookies.clear()
            session._local_storage.clear()
            session._session_storage.clear()
            session._data.clear()
            session._errors.clear()
            session._nav_history.clear()
            session._recovery_attempts = 0
            session.current_url = session.identity.entry_url
            session.started_at = None
            session.completed_at = None
            session.created_at = time.monotonic()

            await session.transition_to(SessionState.READY)
            await self._pool.update_state_index(
                session.session_id, SessionState.RECYCLING, SessionState.READY
            )

            await self._event_bus.publish_simple(
                EventCategory.SESSION_RECOVERED,
                {"session_id": session.session_id, "type": "recycled"},
            )
            logger.debug(f"[SessionManager] Recycled: {session.session_id[:8]}")

        except Exception as exc:
            logger.error(f"[SessionManager] Recycle failed: {exc}")
            await self.destroy_session(session.session_id, "recycle_failed")

    async def _attempt_recovery(self, session: Session, reason: str) -> Session:
        """Attempt to recover a failed session."""
        session.increment_recovery_attempt()

        old = session.state
        await session.transition_to(SessionState.RECOVERING)
        await self._pool.update_state_index(
            session.session_id, old, SessionState.RECOVERING
        )

        logger.info(
            f"[SessionManager] Recovering: {session.session_id[:8]} "
            f"(attempt {session._recovery_attempts}/{session.max_recovery_attempts})"
        )

        # Brief cooldown
        await asyncio.sleep(2.0 * session._recovery_attempts)

        await session.transition_to(SessionState.READY)
        await self._pool.update_state_index(
            session.session_id, SessionState.RECOVERING, SessionState.READY
        )

        self._total_sessions_recovered += 1

        await self._event_bus.publish_simple(
            EventCategory.SESSION_RECOVERED,
            {
                "session_id": session.session_id,
                "attempt":    session._recovery_attempts,
                "reason":     reason,
            },
        )
        return session

    async def _health_monitor_loop(self) -> None:
        """Background loop to detect and handle expired/stuck sessions."""
        while self._running:
            try:
                await asyncio.sleep(self._health_check_interval)
                await self._run_health_checks()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error(f"[SessionManager] Health monitor error: {exc}")

    async def _run_health_checks(self) -> None:
        """Check all sessions for expiry, stuck states, or corruption."""
        all_sessions = self._pool.get_all()
        tasks = []

        for session in all_sessions:
            if session.is_terminal:
                tasks.append(
                    self._pool.remove(session.session_id)
                )
                continue

            if session.is_expired:
                logger.info(
                    f"[SessionManager] Session expired: {session.session_id[:8]}"
                )
                await self._event_bus.publish_simple(
                    EventCategory.SESSION_EXPIRED,
                    {
                        "session_id": session.session_id,
                        "duration":   round(session.duration, 2),
                    },
                )
                tasks.append(
                    self.destroy_session(session.session_id, "expired")
                )

            elif session.time_since_active > 120:
                # Session stuck idle for > 2 minutes
                logger.warning(
                    f"[SessionManager] Session idle too long: "
                    f"{session.session_id[:8]} ({session.time_since_active:.0f}s)"
                )
                tasks.append(
                    self.complete_session(session.session_id, success=False)
                )

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)