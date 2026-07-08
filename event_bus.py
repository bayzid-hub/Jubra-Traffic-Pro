"""
Jubra Traffic Pro - Async Event Bus
High-performance event-driven architecture with priority queues,
dead letter queues, event replay, and distributed pub/sub support.
"""

import asyncio
import time
import uuid
import json
import weakref
import hashlib
import logging
from enum import IntEnum, auto
from dataclasses import dataclass, field
from typing import (
    Any, Callable, Coroutine, Dict, List, Optional,
    Set, Tuple, Type, Union
)
from collections import defaultdict, deque

logger = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Event Priority System
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class EventPriority(IntEnum):
    """Priority levels for event processing."""
    CRITICAL    = 0   # System-critical: shutdown, crash recovery
    HIGH        = 10  # Urgent: detection, CAPTCHA, proxy failure
    NORMAL      = 20  # Standard: traffic events, session lifecycle
    LOW         = 30  # Background: metrics, logging, analytics
    BACKGROUND  = 40  # Deferred: cleanup, optimization tasks


class EventStatus:
    """Event lifecycle status constants."""
    PENDING     = "pending"
    PROCESSING  = "processing"
    COMPLETED   = "completed"
    FAILED      = "failed"
    RETRYING    = "retrying"
    DEAD        = "dead"
    CANCELLED   = "cancelled"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Event Categories (strongly typed)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class EventCategory:
    """Namespaced event category constants."""

    # System lifecycle
    SYSTEM_STARTUP          = "system.startup"
    SYSTEM_SHUTDOWN         = "system.shutdown"
    SYSTEM_READY            = "system.ready"
    SYSTEM_ERROR            = "system.error"
    SYSTEM_RESOURCE_WARNING = "system.resource_warning"

    # Session lifecycle
    SESSION_CREATED         = "session.created"
    SESSION_STARTED         = "session.started"
    SESSION_COMPLETED       = "session.completed"
    SESSION_FAILED          = "session.failed"
    SESSION_EXPIRED         = "session.expired"
    SESSION_RECOVERED       = "session.recovered"

    # Proxy events
    PROXY_ACQUIRED          = "proxy.acquired"
    PROXY_RELEASED          = "proxy.released"
    PROXY_FAILED            = "proxy.failed"
    PROXY_BANNED            = "proxy.banned"
    PROXY_HEALTH_UPDATE     = "proxy.health_update"
    PROXY_POOL_LOW          = "proxy.pool_low"
    PROXY_POOL_EXHAUSTED    = "proxy.pool_exhausted"
    PROXY_ROTATED           = "proxy.rotated"

    # Browser events
    BROWSER_LAUNCHED        = "browser.launched"
    BROWSER_CRASHED         = "browser.crashed"
    BROWSER_RECYCLED        = "browser.recycled"
    BROWSER_POOL_WARMED     = "browser.pool_warmed"
    PAGE_LOADED             = "browser.page_loaded"
    PAGE_LOAD_FAILED        = "browser.page_load_failed"
    SCREENSHOT_TAKEN        = "browser.screenshot_taken"

    # Fingerprint events
    FINGERPRINT_GENERATED   = "fingerprint.generated"
    FINGERPRINT_APPLIED     = "fingerprint.applied"
    FINGERPRINT_MUTATED     = "fingerprint.mutated"
    FINGERPRINT_INCONSISTENT = "fingerprint.inconsistent"

    # Traffic events
    TRAFFIC_VISIT_START     = "traffic.visit_start"
    TRAFFIC_VISIT_COMPLETE  = "traffic.visit_complete"
    TRAFFIC_VISIT_FAILED    = "traffic.visit_failed"
    TRAFFIC_GOAL_REACHED    = "traffic.goal_reached"
    TRAFFIC_BOUNCE          = "traffic.bounce"
    TRAFFIC_CONVERSION      = "traffic.conversion"

    # Detection / Security events
    DETECTION_BOT_DETECTED  = "detection.bot_detected"
    DETECTION_CAPTCHA       = "detection.captcha"
    DETECTION_CAPTCHA_SOLVED = "detection.captcha_solved"
    DETECTION_IP_BLOCKED    = "detection.ip_blocked"
    DETECTION_RATE_LIMITED  = "detection.rate_limited"
    DETECTION_CLOUDFLARE    = "detection.cloudflare"
    DETECTION_HONEYPOT      = "detection.honeypot"
    DETECTION_EVADED        = "detection.evaded"

    # Behavior events
    BEHAVIOR_ACTION         = "behavior.action"
    BEHAVIOR_SCROLL         = "behavior.scroll"
    BEHAVIOR_CLICK          = "behavior.click"
    BEHAVIOR_FORM_FILL      = "behavior.form_fill"
    BEHAVIOR_IDLE           = "behavior.idle"
    BEHAVIOR_EXIT_INTENT    = "behavior.exit_intent"

    # Analytics events
    ANALYTICS_GA4_EVENT     = "analytics.ga4_event"
    ANALYTICS_PIXEL_FIRE    = "analytics.pixel_fire"
    ANALYTICS_HEATMAP       = "analytics.heatmap"

    # Monitoring events
    METRICS_UPDATE          = "metrics.update"
    PERFORMANCE_SAMPLE      = "metrics.performance_sample"
    HEALTH_CHECK            = "metrics.health_check"
    ALERT_TRIGGERED         = "metrics.alert"

    # Self-healing events
    HEALING_TRIGGERED       = "healing.triggered"
    HEALING_SUCCESS         = "healing.success"
    HEALING_FAILED          = "healing.failed"

    # Config events
    CONFIG_LOADED           = "config.loaded"
    CONFIG_RELOADED         = "config.reloaded"
    CONFIG_ERROR            = "config.error"

    # GUI events
    GUI_UPDATE              = "gui.update"
    GUI_CHART_DATA          = "gui.chart_data"
    GUI_LOG_ENTRY           = "gui.log_entry"
    GUI_ALERT               = "gui.alert"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Event Data Model
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@dataclass
class Event:
    """
    Core event data model.
    Immutable after creation for thread safety.
    """
    category:       str
    data:           Dict[str, Any]              = field(default_factory=dict)
    priority:       EventPriority               = EventPriority.NORMAL
    source:         str                         = ""
    session_id:     Optional[str]               = None
    correlation_id: Optional[str]               = None
    tags:           Set[str]                    = field(default_factory=set)
    ttl:            Optional[float]             = None    # seconds until expiry
    retry_count:    int                         = 0
    max_retries:    int                         = 3

    # Auto-generated fields
    event_id:       str                         = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp:      float                       = field(default_factory=time.monotonic)
    wall_time:      float                       = field(default_factory=time.time)
    status:         str                         = field(default=EventStatus.PENDING)

    def __post_init__(self):
        # Ensure tags is a set, not a list
        if isinstance(self.tags, list):
            self.tags = set(self.tags)

    @property
    def is_expired(self) -> bool:
        """Check if event has exceeded its TTL."""
        if self.ttl is None:
            return False
        return (time.monotonic() - self.timestamp) > self.ttl

    @property
    def age_ms(self) -> float:
        """Event age in milliseconds."""
        return (time.monotonic() - self.timestamp) * 1000

    @property
    def fingerprint(self) -> str:
        """Stable fingerprint for deduplication."""
        data = f"{self.category}:{self.source}:{sorted(self.data.keys())}"
        return hashlib.md5(data.encode()).hexdigest()[:8]

    def with_data(self, **kwargs) -> "Event":
        """Create a new event with updated data."""
        new_data = {**self.data, **kwargs}
        import dataclasses
        return dataclasses.replace(self, data=new_data)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "event_id":       self.event_id,
            "category":       self.category,
            "data":           self.data,
            "priority":       self.priority.name,
            "source":         self.source,
            "session_id":     self.session_id,
            "correlation_id": self.correlation_id,
            "tags":           list(self.tags),
            "ttl":            self.ttl,
            "retry_count":    self.retry_count,
            "status":         self.status,
            "timestamp":      self.timestamp,
            "wall_time":      self.wall_time,
            "age_ms":         self.age_ms,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), default=str)

    def __lt__(self, other: "Event") -> bool:
        """Priority queue comparison: lower priority value = higher priority."""
        if self.priority != other.priority:
            return self.priority < other.priority
        return self.timestamp < other.timestamp

    def __repr__(self) -> str:
        return (
            f"Event(id={self.event_id[:8]}, "
            f"category={self.category!r}, "
            f"priority={self.priority.name}, "
            f"age={self.age_ms:.1f}ms)"
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Handler Types
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

HandlerFunc = Union[
    Callable[[Event], None],
    Callable[[Event], Coroutine[Any, Any, None]],
]


@dataclass
class EventHandler:
    """
    Registered event handler with metadata.
    Supports both sync and async handlers.
    """
    handler_id:   str
    handler:      HandlerFunc
    category:     str
    priority:     int               = 0      # Handler priority (higher = first)
    is_async:     bool              = False
    is_wildcard:  bool              = False  # Matches any category
    filter_func:  Optional[Callable[[Event], bool]] = None
    max_calls:    Optional[int]     = None   # None = unlimited
    call_count:   int               = 0
    error_count:  int               = 0
    is_active:    bool              = True
    source_tag:   str               = ""     # Source module identifier
    created_at:   float             = field(default_factory=time.monotonic)

    @property
    def is_exhausted(self) -> bool:
        """Check if handler has reached its max call limit."""
        if self.max_calls is None:
            return False
        return self.call_count >= self.max_calls

    @property
    def success_rate(self) -> float:
        if self.call_count == 0:
            return 1.0
        return max(0.0, (self.call_count - self.error_count) / self.call_count)

    def matches(self, event: Event) -> bool:
        """Check if this handler should receive the given event."""
        if not self.is_active or self.is_exhausted:
            return False
        if event.is_expired:
            return False
        if not self.is_wildcard and not self._category_matches(event.category):
            return False
        if self.filter_func and not self.filter_func(event):
            return False
        return True

    def _category_matches(self, event_category: str) -> bool:
        """Support wildcard matching like 'proxy.*' or 'session.*'."""
        if self.category == event_category:
            return True
        # Support namespace wildcards
        if self.category.endswith(".*"):
            prefix = self.category[:-2]
            return event_category.startswith(prefix + ".")
        return False

    def __repr__(self) -> str:
        return (
            f"EventHandler(id={self.handler_id[:8]}, "
            f"category={self.category!r}, "
            f"calls={self.call_count}, "
            f"active={self.is_active})"
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Dead Letter Queue
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class DeadLetterQueue:
    """
    Stores events that failed all processing attempts.
    Supports inspection, replay, and export.
    """

    def __init__(self, max_size: int = 5000):
        self._queue: deque = deque(maxlen=max_size)
        self._failure_reasons: Dict[str, str] = {}
        self._lock = asyncio.Lock()

    async def put(self, event: Event, reason: str) -> None:
        """Add a dead event to the DLQ."""
        async with self._lock:
            dead_event = Event(
                category=event.category,
                data=event.data,
                priority=event.priority,
                source=event.source,
                session_id=event.session_id,
                correlation_id=event.correlation_id,
                tags=event.tags | {"dead_letter"},
                retry_count=event.retry_count,
                status=EventStatus.DEAD,
            )
            self._queue.append(dead_event)
            self._failure_reasons[dead_event.event_id] = reason
            logger.warning(
                f"[DLQ] Event dead-lettered: {event.category} "
                f"| ID: {event.event_id[:8]} | Reason: {reason}"
            )

    async def get_all(self) -> List[Tuple[Event, str]]:
        """Get all dead-lettered events with their failure reasons."""
        async with self._lock:
            return [
                (evt, self._failure_reasons.get(evt.event_id, "unknown"))
                for evt in self._queue
            ]

    async def replay(self, bus: "EventBus", category_filter: Optional[str] = None) -> int:
        """Replay dead-lettered events back to the bus."""
        replayed = 0
        async with self._lock:
            to_replay = list(self._queue)
        for event in to_replay:
            if category_filter and event.category != category_filter:
                continue
            # Reset retry count and status for replay
            import dataclasses
            fresh = dataclasses.replace(
                event,
                event_id=str(uuid.uuid4()),
                retry_count=0,
                status=EventStatus.PENDING,
                tags=event.tags - {"dead_letter"},
            )
            await bus.publish(fresh)
            replayed += 1
        return replayed

    async def clear(self) -> None:
        async with self._lock:
            self._queue.clear()
            self._failure_reasons.clear()

    @property
    def size(self) -> int:
        return len(self._queue)

    def export_json(self) -> str:
        """Export all DLQ entries as JSON."""
        entries = [
            {
                "event": evt.to_dict(),
                "reason": self._failure_reasons.get(evt.event_id, "unknown"),
            }
            for evt in self._queue
        ]
        return json.dumps(entries, indent=2, default=str)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Event Replay Buffer
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class EventReplayBuffer:
    """
    Circular buffer of recent events for late subscriber replay.
    Allows new subscribers to catch up on missed events.
    """

    def __init__(self, max_size: int = 2000):
        self._buffer: deque = deque(maxlen=max_size)
        self._lock = asyncio.Lock()

    async def record(self, event: Event) -> None:
        async with self._lock:
            self._buffer.append(event)

    async def get_since(
        self,
        since_timestamp: float,
        category_filter: Optional[str] = None,
    ) -> List[Event]:
        """Get events since a given timestamp."""
        async with self._lock:
            events = [
                evt for evt in self._buffer
                if evt.timestamp > since_timestamp
                and (category_filter is None or evt.category == category_filter)
            ]
        return events

    async def get_last_n(self, n: int, category_filter: Optional[str] = None) -> List[Event]:
        async with self._lock:
            all_events = list(self._buffer)
        if category_filter:
            all_events = [e for e in all_events if e.category == category_filter]
        return all_events[-n:]

    async def clear(self) -> None:
        async with self._lock:
            self._buffer.clear()

    @property
    def size(self) -> int:
        return len(self._buffer)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Event Bus Metrics
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class EventBusMetrics:
    """Real-time metrics for the event bus."""

    def __init__(self):
        self.total_published:       int   = 0
        self.total_processed:       int   = 0
        self.total_failed:          int   = 0
        self.total_dead_lettered:   int   = 0
        self.total_expired:         int   = 0
        self.total_filtered:        int   = 0
        self._category_counts:      Dict[str, int] = defaultdict(int)
        self._processing_times_ms:  deque = deque(maxlen=1000)
        self._queue_size_history:   deque = deque(maxlen=500)
        self._start_time:           float = time.monotonic()

    def record_published(self, category: str) -> None:
        self.total_published += 1
        self._category_counts[category] += 1

    def record_processed(self, processing_time_ms: float) -> None:
        self.total_processed += 1
        self._processing_times_ms.append(processing_time_ms)

    def record_failed(self) -> None:
        self.total_failed += 1

    def record_dead_lettered(self) -> None:
        self.total_dead_lettered += 1

    def record_expired(self) -> None:
        self.total_expired += 1

    def record_queue_size(self, size: int) -> None:
        self._queue_size_history.append((time.monotonic(), size))

    @property
    def avg_processing_time_ms(self) -> float:
        if not self._processing_times_ms:
            return 0.0
        return sum(self._processing_times_ms) / len(self._processing_times_ms)

    @property
    def p99_processing_time_ms(self) -> float:
        if not self._processing_times_ms:
            return 0.0
        sorted_times = sorted(self._processing_times_ms)
        idx = int(len(sorted_times) * 0.99)
        return sorted_times[min(idx, len(sorted_times) - 1)]

    @property
    def events_per_second(self) -> float:
        elapsed = time.monotonic() - self._start_time
        if elapsed == 0:
            return 0.0
        return self.total_published / elapsed

    @property
    def success_rate(self) -> float:
        if self.total_processed == 0:
            return 1.0
        return (self.total_processed - self.total_failed) / self.total_processed

    def get_hot_categories(self, top_n: int = 5) -> List[Tuple[str, int]]:
        return sorted(
            self._category_counts.items(),
            key=lambda x: x[1],
            reverse=True,
        )[:top_n]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total_published":        self.total_published,
            "total_processed":        self.total_processed,
            "total_failed":           self.total_failed,
            "total_dead_lettered":    self.total_dead_lettered,
            "total_expired":          self.total_expired,
            "events_per_second":      round(self.events_per_second, 2),
            "success_rate":           round(self.success_rate, 4),
            "avg_processing_ms":      round(self.avg_processing_time_ms, 2),
            "p99_processing_ms":      round(self.p99_processing_time_ms, 2),
            "hot_categories":         self.get_hot_categories(),
        }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Priority Event Queue
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class PriorityEventQueue:
    """
    Async priority queue for events.
    Uses per-priority FIFO queues for deterministic ordering.
    """

    def __init__(self, max_size: int = 50000):
        self._queues: Dict[EventPriority, asyncio.Queue] = {
            p: asyncio.Queue() for p in EventPriority
        }
        self._max_size = max_size
        self._total_size = 0
        self._lock = asyncio.Lock()
        # Semaphore to signal when any event is available
        self._available = asyncio.Semaphore(0)

    async def put(self, event: Event) -> bool:
        """Add event to appropriate priority queue. Returns False if full."""
        async with self._lock:
            if self._total_size >= self._max_size:
                logger.warning(
                    f"[EventQueue] Queue full ({self._total_size}), "
                    f"dropping: {event.category}"
                )
                return False
            self._total_size += 1

        await self._queues[event.priority].put(event)
        self._available.release()
        return True

    async def get(self, timeout: float = 1.0) -> Optional[Event]:
        """Get highest-priority event. Returns None on timeout."""
        try:
            await asyncio.wait_for(self._available.acquire(), timeout=timeout)
        except asyncio.TimeoutError:
            return None

        # Check queues from highest to lowest priority
        for priority in EventPriority:
            queue = self._queues[priority]
            if not queue.empty():
                event = await queue.get()
                async with self._lock:
                    self._total_size -= 1
                return event
        return None

    async def get_nowait(self) -> Optional[Event]:
        """Non-blocking get. Returns None if no events available."""
        for priority in EventPriority:
            queue = self._queues[priority]
            if not queue.empty():
                try:
                    event = queue.get_nowait()
                    async with self._lock:
                        self._total_size -= 1
                    return event
                except asyncio.QueueEmpty:
                    continue
        return None

    @property
    def size(self) -> int:
        return self._total_size

    @property
    def is_empty(self) -> bool:
        return self._total_size == 0

    def size_by_priority(self) -> Dict[str, int]:
        return {
            p.name: self._queues[p].qsize()
            for p in EventPriority
        }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Core Event Bus
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class EventBus:
    """
    Jubra Traffic Pro - Core Async Event Bus

    Features:
    ─────────
    • Priority-based async dispatch (CRITICAL → BACKGROUND)
    • Wildcard category subscription (e.g., 'proxy.*')
    • Dead Letter Queue for failed events
    • Event Replay Buffer for late subscribers
    • Per-handler error isolation
    • One-time handler support
    • Handler filtering via custom predicates
    • Middleware pipeline
    • Real-time metrics
    • Graceful shutdown with drain
    • Event correlation tracking
    • Deduplication support
    • Multiple worker coroutines per priority
    """

    def __init__(
        self,
        worker_count:       int   = 8,
        max_queue_size:     int   = 50000,
        dlq_max_size:       int   = 5000,
        replay_buffer_size: int   = 2000,
        handler_timeout:    float = 30.0,
        enable_dedup:       bool  = False,
        dedup_window_ms:    float = 100.0,
    ):
        self._handlers:         Dict[str, List[EventHandler]] = defaultdict(list)
        self._wildcard_handlers: List[EventHandler]           = []
        self._middleware:       List[Callable]                = []
        self._queue             = PriorityEventQueue(max_queue_size)
        self._dlq               = DeadLetterQueue(dlq_max_size)
        self._replay_buffer     = EventReplayBuffer(replay_buffer_size)
        self._metrics           = EventBusMetrics()

        self._worker_count      = worker_count
        self._handler_timeout   = handler_timeout
        self._enable_dedup      = enable_dedup
        self._dedup_window_ms   = dedup_window_ms
        self._recent_fingerprints: Dict[str, float] = {}

        self._workers:          List[asyncio.Task] = []
        self._running:          bool               = False
        self._shutting_down:    bool               = False
        self._lock              = asyncio.Lock()

        # Event interceptors (called before dispatch)
        self._interceptors:     List[Callable[[Event], Optional[Event]]] = []

        # Correlation tracking
        self._correlations:     Dict[str, List[str]] = defaultdict(list)

        logger.info(
            f"[EventBus] Initialized: workers={worker_count}, "
            f"queue_max={max_queue_size}, dedup={enable_dedup}"
        )

    # ── Lifecycle ──────────────────────────────────────────

    async def start(self) -> None:
        """Start the event bus workers."""
        if self._running:
            logger.warning("[EventBus] Already running")
            return

        self._running = True
        self._shutting_down = False

        # Launch worker pool
        for i in range(self._worker_count):
            task = asyncio.create_task(
                self._worker_loop(worker_id=i),
                name=f"EventBus-Worker-{i}",
            )
            self._workers.append(task)

        # Launch metrics sampler
        self._metric_task = asyncio.create_task(
            self._metrics_sampler(),
            name="EventBus-Metrics",
        )

        # Launch dedup cleaner
        if self._enable_dedup:
            self._dedup_task = asyncio.create_task(
                self._dedup_cleaner(),
                name="EventBus-Dedup",
            )

        logger.info(f"[EventBus] Started with {self._worker_count} workers")
        await self.publish_simple(EventCategory.SYSTEM_STARTUP, {"workers": self._worker_count})

    async def stop(self, drain_timeout: float = 10.0) -> None:
        """Gracefully stop the event bus."""
        if not self._running:
            return

        logger.info("[EventBus] Shutting down...")
        self._shutting_down = True

        await self.publish_simple(
            EventCategory.SYSTEM_SHUTDOWN,
            {"drain_timeout": drain_timeout},
            priority=EventPriority.CRITICAL,
        )

        # Wait for queue to drain
        drain_start = time.monotonic()
        while not self._queue.is_empty:
            if (time.monotonic() - drain_start) > drain_timeout:
                logger.warning(
                    f"[EventBus] Drain timeout. "
                    f"Remaining: {self._queue.size} events"
                )
                break
            await asyncio.sleep(0.1)

        # Cancel all workers
        self._running = False
        for task in self._workers:
            task.cancel()
        if hasattr(self, "_metric_task"):
            self._metric_task.cancel()
        if self._enable_dedup and hasattr(self, "_dedup_task"):
            self._dedup_task.cancel()

        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()

        logger.info(
            f"[EventBus] Stopped. Metrics: {self._metrics.to_dict()}"
        )

    # ── Publishing ─────────────────────────────────────────

    async def publish(self, event: Event) -> bool:
        """
        Publish an event to the bus.
        Returns True if accepted, False if rejected/dropped.
        """
        if self._shutting_down and event.priority != EventPriority.CRITICAL:
            return False

        # Run through interceptors
        processed_event = await self._run_interceptors(event)
        if processed_event is None:
            self._metrics.total_filtered += 1
            return False

        # Deduplication check
        if self._enable_dedup and self._is_duplicate(processed_event):
            logger.debug(f"[EventBus] Dedup dropped: {event.fingerprint}")
            return False

        # Record to replay buffer
        await self._replay_buffer.record(processed_event)

        # Track correlation
        if processed_event.correlation_id:
            self._correlations[processed_event.correlation_id].append(
                processed_event.event_id
            )

        # Enqueue
        accepted = await self._queue.put(processed_event)
        if accepted:
            self._metrics.record_published(processed_event.category)

        return accepted

    async def publish_simple(
        self,
        category:       str,
        data:           Optional[Dict[str, Any]] = None,
        priority:       EventPriority            = EventPriority.NORMAL,
        source:         str                      = "",
        session_id:     Optional[str]            = None,
        correlation_id: Optional[str]            = None,
        tags:           Optional[Set[str]]        = None,
        ttl:            Optional[float]           = None,
    ) -> bool:
        """Convenience method to publish without manually creating an Event."""
        event = Event(
            category=category,
            data=data or {},
            priority=priority,
            source=source,
            session_id=session_id,
            correlation_id=correlation_id,
            tags=tags or set(),
            ttl=ttl,
        )
        return await self.publish(event)

    async def publish_batch(self, events: List[Event]) -> int:
        """Publish multiple events at once. Returns count accepted."""
        accepted = 0
        for event in events:
            if await self.publish(event):
                accepted += 1
        return accepted

    # ── Subscription ───────────────────────────────────────

    def subscribe(
        self,
        category:    str,
        handler:     HandlerFunc,
        priority:    int                            = 0,
        filter_func: Optional[Callable[[Event], bool]] = None,
        max_calls:   Optional[int]                  = None,
        source_tag:  str                            = "",
        replay_from: Optional[float]                = None,
    ) -> str:
        """
        Subscribe to an event category.
        Returns handler_id for later unsubscription.

        Args:
            category:    Event category or wildcard (e.g., 'proxy.*')
            handler:     Sync or async callable(event: Event) -> None
            priority:    Handler execution priority (higher = earlier)
            filter_func: Optional predicate to filter events
            max_calls:   Maximum calls before auto-unsubscribe
            source_tag:  Module identifier for debugging
            replay_from: Timestamp to replay buffered events from
        """
        handler_id = str(uuid.uuid4())
        is_async = asyncio.iscoroutinefunction(handler)
        is_wildcard = category == "*" or category.endswith(".*")

        eh = EventHandler(
            handler_id=handler_id,
            handler=handler,
            category=category,
            priority=priority,
            is_async=is_async,
            is_wildcard=is_wildcard,
            filter_func=filter_func,
            max_calls=max_calls,
            source_tag=source_tag,
        )

        if is_wildcard:
            self._wildcard_handlers.append(eh)
            self._wildcard_handlers.sort(key=lambda h: h.priority, reverse=True)
        else:
            self._handlers[category].append(eh)
            self._handlers[category].sort(key=lambda h: h.priority, reverse=True)

        logger.debug(
            f"[EventBus] Subscribed: category={category!r}, "
            f"id={handler_id[:8]}, async={is_async}, tag={source_tag!r}"
        )

        # Schedule replay if requested
        if replay_from is not None and self._running:
            asyncio.create_task(self._replay_to_handler(eh, replay_from))

        return handler_id

    def subscribe_once(
        self,
        category:   str,
        handler:    HandlerFunc,
        source_tag: str = "",
    ) -> str:
        """Subscribe for exactly one event delivery."""
        return self.subscribe(
            category=category,
            handler=handler,
            max_calls=1,
            source_tag=source_tag or "once",
        )

    def unsubscribe(self, handler_id: str) -> bool:
        """Remove a handler by ID. Returns True if found and removed."""
        # Check regular handlers
        for category, handlers in self._handlers.items():
            for i, h in enumerate(handlers):
                if h.handler_id == handler_id:
                    h.is_active = False
                    self._handlers[category].pop(i)
                    logger.debug(f"[EventBus] Unsubscribed: {handler_id[:8]}")
                    return True

        # Check wildcard handlers
        for i, h in enumerate(self._wildcard_handlers):
            if h.handler_id == handler_id:
                h.is_active = False
                self._wildcard_handlers.pop(i)
                logger.debug(f"[EventBus] Unsubscribed wildcard: {handler_id[:8]}")
                return True

        return False

    def unsubscribe_all(self, source_tag: str) -> int:
        """Remove all handlers from a specific source module."""
        removed = 0
        for handlers in self._handlers.values():
            to_remove = [h for h in handlers if h.source_tag == source_tag]
            for h in to_remove:
                h.is_active = False
                handlers.remove(h)
                removed += 1
        wc_remove = [h for h in self._wildcard_handlers if h.source_tag == source_tag]
        for h in wc_remove:
            h.is_active = False
            self._wildcard_handlers.remove(h)
            removed += 1
        return removed

    # ── Middleware ─────────────────────────────────────────

    def add_middleware(self, middleware: Callable[[Event], Optional[Event]]) -> None:
        """
        Add a middleware function to the processing pipeline.
        Middleware can modify events or return None to drop them.
        """
        self._middleware.append(middleware)

    def add_interceptor(self, interceptor: Callable[[Event], Optional[Event]]) -> None:
        """
        Add pre-publish interceptor.
        Interceptors run before events enter the queue.
        Return None to discard the event.
        """
        self._interceptors.append(interceptor)

    # ── Wait For ───────────────────────────────────────────

    async def wait_for(
        self,
        category:   str,
        timeout:    float                          = 30.0,
        filter_func: Optional[Callable[[Event], bool]] = None,
    ) -> Optional[Event]:
        """
        Wait for a specific event category.
        Returns the event or None on timeout.
        """
        future: asyncio.Future = asyncio.get_event_loop().create_future()

        def _on_event(event: Event) -> None:
            if not future.done():
                future.set_result(event)

        handler_id = self.subscribe(
            category=category,
            handler=_on_event,
            max_calls=1,
            filter_func=filter_func,
            source_tag="wait_for",
        )

        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            self.unsubscribe(handler_id)
            return None

    async def wait_for_any(
        self,
        categories: List[str],
        timeout:    float = 30.0,
    ) -> Optional[Event]:
        """Wait for the first event matching any of the given categories."""
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        handler_ids = []

        def _on_event(event: Event) -> None:
            if not future.done():
                future.set_result(event)

        for category in categories:
            hid = self.subscribe(
                category=category,
                handler=_on_event,
                max_calls=1,
                source_tag="wait_for_any",
            )
            handler_ids.append(hid)

        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            for hid in handler_ids:
                self.unsubscribe(hid)
            return None

    # ── Replay ─────────────────────────────────────────────

    async def replay_events(
        self,
        since_timestamp:  float,
        category_filter:  Optional[str] = None,
    ) -> int:
        """Replay buffered events since a given timestamp."""
        events = await self._replay_buffer.get_since(since_timestamp, category_filter)
        count = 0
        for event in events:
            import dataclasses
            fresh = dataclasses.replace(
                event,
                event_id=str(uuid.uuid4()),
                timestamp=time.monotonic(),
                status=EventStatus.PENDING,
                retry_count=0,
            )
            await self._queue.put(fresh)
            count += 1
        logger.info(f"[EventBus] Replayed {count} events since {since_timestamp}")
        return count

    # ── Query ──────────────────────────────────────────────

    def get_handlers_for(self, category: str) -> List[EventHandler]:
        """Get all active handlers for a category."""
        specific = [h for h in self._handlers.get(category, []) if h.is_active]
        wildcards = [h for h in self._wildcard_handlers if h.matches(Event(category=category))]
        return sorted(specific + wildcards, key=lambda h: h.priority, reverse=True)

    def get_correlated_events(self, correlation_id: str) -> List[str]:
        """Get all event IDs with a given correlation ID."""
        return self._correlations.get(correlation_id, [])

    @property
    def metrics(self) -> EventBusMetrics:
        return self._metrics

    @property
    def dlq(self) -> DeadLetterQueue:
        return self._dlq

    @property
    def replay_buffer(self) -> EventReplayBuffer:
        return self._replay_buffer

    @property
    def queue_size(self) -> int:
        return self._queue.size

    @property
    def is_running(self) -> bool:
        return self._running

    def get_status(self) -> Dict[str, Any]:
        """Get full bus status snapshot."""
        return {
            "running":          self._running,
            "shutting_down":    self._shutting_down,
            "worker_count":     len(self._workers),
            "queue_size":       self._queue.size,
            "queue_by_priority": self._queue.size_by_priority(),
            "dlq_size":         self._dlq.size,
            "replay_buffer":    self._replay_buffer.size,
            "handler_count":    sum(len(v) for v in self._handlers.values()),
            "wildcard_handlers": len(self._wildcard_handlers),
            "metrics":          self._metrics.to_dict(),
        }

    # ── Internal Worker Loop ───────────────────────────────

    async def _worker_loop(self, worker_id: int) -> None:
        """Main event processing loop for a single worker."""
        logger.debug(f"[EventBus] Worker {worker_id} started")

        while self._running:
            try:
                event = await self._queue.get(timeout=0.5)
                if event is None:
                    continue

                # Check expiry
                if event.is_expired:
                    self._metrics.record_expired()
                    logger.debug(f"[EventBus] Expired: {event.category}")
                    continue

                # Process event
                start_ts = time.monotonic()
                await self._dispatch(event)
                elapsed_ms = (time.monotonic() - start_ts) * 1000
                self._metrics.record_processed(elapsed_ms)

            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.exception(
                    f"[EventBus] Worker {worker_id} unexpected error: {exc}"
                )
                await asyncio.sleep(0.1)

        logger.debug(f"[EventBus] Worker {worker_id} stopped")

    async def _dispatch(self, event: Event) -> None:
        """Dispatch event to all matching handlers."""
        # Collect matching handlers
        handlers = []
        specific = self._handlers.get(event.category, [])
        for h in specific:
            if h.matches(event):
                handlers.append(h)
        for h in self._wildcard_handlers:
            if h.matches(event):
                handlers.append(h)

        if not handlers:
            return

        # Sort by priority
        handlers.sort(key=lambda h: h.priority, reverse=True)

        # Run middleware pipeline
        current_event = event
        for mw in self._middleware:
            try:
                result = mw(current_event)
                if asyncio.iscoroutine(result):
                    result = await result
                if result is None:
                    return  # Middleware dropped the event
                current_event = result
            except Exception as exc:
                logger.error(f"[EventBus] Middleware error: {exc}")

        # Dispatch to each handler
        for handler in handlers:
            await self._call_handler(handler, current_event)

            # Auto-remove exhausted handlers
            if handler.is_exhausted:
                handler.is_active = False

    async def _call_handler(self, handler: EventHandler, event: Event) -> None:
        """Invoke a single handler with timeout and error isolation."""
        handler.call_count += 1
        try:
            if handler.is_async:
                await asyncio.wait_for(
                    handler.handler(event),
                    timeout=self._handler_timeout,
                )
            else:
                # Run sync handlers in thread pool
                loop = asyncio.get_event_loop()
                await asyncio.wait_for(
                    loop.run_in_executor(None, handler.handler, event),
                    timeout=self._handler_timeout,
                )
        except asyncio.TimeoutError:
            handler.error_count += 1
            logger.error(
                f"[EventBus] Handler timeout: {handler.handler_id[:8]} "
                f"for {event.category}"
            )
            await self._handle_failure(event, f"Handler timeout after {self._handler_timeout}s")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            handler.error_count += 1
            logger.error(
                f"[EventBus] Handler error: {handler.handler_id[:8]} "
                f"for {event.category}: {exc}",
                exc_info=True,
            )
            await self._handle_failure(event, str(exc))

    async def _handle_failure(self, event: Event, reason: str) -> None:
        """Handle event processing failure with retry logic."""
        self._metrics.record_failed()

        if event.retry_count < event.max_retries:
            # Retry with backoff
            import dataclasses
            retry_delay = 0.5 * (2 ** event.retry_count)
            retried_event = dataclasses.replace(
                event,
                retry_count=event.retry_count + 1,
                status=EventStatus.RETRYING,
            )
            logger.debug(
                f"[EventBus] Retrying event {event.event_id[:8]} "
                f"(attempt {retried_event.retry_count}/{event.max_retries}) "
                f"after {retry_delay}s"
            )
            await asyncio.sleep(retry_delay)
            await self._queue.put(retried_event)
        else:
            # Send to DLQ
            self._metrics.record_dead_lettered()
            await self._dlq.put(event, reason)

    async def _run_interceptors(self, event: Event) -> Optional[Event]:
        """Run all pre-publish interceptors."""
        current = event
        for interceptor in self._interceptors:
            try:
                result = interceptor(current)
                if asyncio.iscoroutine(result):
                    result = await result
                if result is None:
                    return None
                current = result
            except Exception as exc:
                logger.error(f"[EventBus] Interceptor error: {exc}")
        return current

    def _is_duplicate(self, event: Event) -> bool:
        """Check if event is a duplicate within the dedup window."""
        fp = event.fingerprint
        now_ms = time.monotonic() * 1000
        if fp in self._recent_fingerprints:
            last_seen = self._recent_fingerprints[fp]
            if (now_ms - last_seen) < self._dedup_window_ms:
                return True
        self._recent_fingerprints[fp] = now_ms
        return False

    async def _metrics_sampler(self) -> None:
        """Periodically sample queue size for metrics."""
        while self._running:
            try:
                self._metrics.record_queue_size(self._queue.size)
                await asyncio.sleep(1.0)
            except asyncio.CancelledError:
                break

    async def _dedup_cleaner(self) -> None:
        """Periodically clean up old dedup fingerprints."""
        while self._running:
            try:
                cutoff = time.monotonic() * 1000 - self._dedup_window_ms * 10
                expired_keys = [
                    k for k, v in self._recent_fingerprints.items()
                    if v < cutoff
                ]
                for k in expired_keys:
                    del self._recent_fingerprints[k]
                await asyncio.sleep(5.0)
            except asyncio.CancelledError:
                break

    async def _replay_to_handler(
        self,
        handler:        EventHandler,
        since_timestamp: float,
    ) -> None:
        """Replay buffered events to a newly registered handler."""
        events = await self._replay_buffer.get_since(since_timestamp)
        for event in events:
            if handler.matches(event):
                await self._call_handler(handler, event)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Convenience Decorators
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def on_event(
    bus:         EventBus,
    category:    str,
    priority:    int                            = 0,
    filter_func: Optional[Callable[[Event], bool]] = None,
    max_calls:   Optional[int]                  = None,
    source_tag:  str                            = "",
) -> Callable:
    """
    Decorator to register a handler on an EventBus.

    Usage:
        @on_event(bus, EventCategory.PROXY_FAILED)
        async def handle_proxy_failure(event: Event):
            ...
    """
    def decorator(func: HandlerFunc) -> HandlerFunc:
        bus.subscribe(
            category=category,
            handler=func,
            priority=priority,
            filter_func=filter_func,
            max_calls=max_calls,
            source_tag=source_tag or func.__module__,
        )
        return func
    return decorator


def on_any_event(bus: EventBus, source_tag: str = "") -> Callable:
    """Decorator to subscribe to all events."""
    def decorator(func: HandlerFunc) -> HandlerFunc:
        bus.subscribe(
            category="*",
            handler=func,
            source_tag=source_tag or func.__module__,
        )
        return func
    return decorator


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Global Event Bus Singleton
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class EventBusRegistry:
    """
    Registry for managing multiple named event bus instances.
    Allows different subsystems to use isolated buses if needed.
    """

    _buses: Dict[str, EventBus] = {}

    @classmethod
    def create(
        cls,
        name:           str = "default",
        worker_count:   int   = 8,
        max_queue_size: int   = 50000,
        **kwargs,
    ) -> EventBus:
        if name in cls._buses:
            logger.warning(f"[EventBusRegistry] Bus '{name}' already exists, returning existing")
            return cls._buses[name]
        bus = EventBus(
            worker_count=worker_count,
            max_queue_size=max_queue_size,
            **kwargs,
        )
        cls._buses[name] = bus
        return bus

    @classmethod
    def get(cls, name: str = "default") -> Optional[EventBus]:
        return cls._buses.get(name)

    @classmethod
    def get_or_create(cls, name: str = "default", **kwargs) -> EventBus:
        if name not in cls._buses:
            return cls.create(name, **kwargs)
        return cls._buses[name]

    @classmethod
    async def start_all(cls) -> None:
        for bus in cls._buses.values():
            await bus.start()

    @classmethod
    async def stop_all(cls, drain_timeout: float = 10.0) -> None:
        for bus in cls._buses.values():
            await bus.stop(drain_timeout)

    @classmethod
    def all_status(cls) -> Dict[str, Any]:
        return {name: bus.get_status() for name, bus in cls._buses.items()}


# Default global bus instance
_default_bus: Optional[EventBus] = None


def get_event_bus() -> EventBus:
    """Get or create the default global event bus."""
    global _default_bus
    if _default_bus is None:
        _default_bus = EventBusRegistry.get_or_create("default")
    return _default_bus