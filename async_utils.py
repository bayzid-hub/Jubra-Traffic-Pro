"""
Jubra Traffic Pro - Async Utilities
Advanced async helpers including rate limiters,
retry logic, timeouts, and concurrency primitives.
"""

import asyncio
import time
import random
import logging
import functools
from typing import (
    Any, Callable, Coroutine, Dict, List,
    Optional, TypeVar, Tuple
)
from dataclasses import dataclass, field
from collections import deque

logger = logging.getLogger(__name__)

T = TypeVar("T")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Rate Limiter
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class RateLimiter:
    """
    Token bucket rate limiter with burst support.

    Usage:
        limiter = RateLimiter(rate=10, burst=20)
        async with limiter:
            await do_request()
    """

    def __init__(
        self,
        rate:       float,          # tokens per second
        burst:      float = None,   # max burst size (defaults to rate)
        jitter:     float = 0.1,    # random jitter factor
    ):
        self._rate      = rate
        self._burst     = burst or rate
        self._tokens    = self._burst
        self._last_time = time.monotonic()
        self._jitter    = jitter
        self._lock      = asyncio.Lock()
        self._waiters:  int = 0

    async def acquire(self, tokens: float = 1.0) -> float:
        """
        Acquire tokens, waiting if necessary.
        Returns wait time in seconds.
        """
        async with self._lock:
            self._waiters += 1
            try:
                while True:
                    now         = time.monotonic()
                    elapsed     = now - self._last_time
                    self._tokens = min(
                        self._burst,
                        self._tokens + elapsed * self._rate,
                    )
                    self._last_time = now

                    if self._tokens >= tokens:
                        self._tokens -= tokens
                        # Add jitter
                        if self._jitter > 0:
                            jitter_s = random.uniform(
                                0, self._jitter / self._rate
                            )
                            await asyncio.sleep(jitter_s)
                        return 0.0

                    # Calculate wait time
                    deficit     = tokens - self._tokens
                    wait_time   = deficit / self._rate
                    await asyncio.sleep(wait_time)
            finally:
                self._waiters -= 1

    async def __aenter__(self) -> "RateLimiter":
        await self.acquire()
        return self

    async def __aexit__(self, *args) -> None:
        pass

    @property
    def available_tokens(self) -> float:
        """Currently available tokens."""
        now     = time.monotonic()
        elapsed = now - self._last_time
        return min(self._burst, self._tokens + elapsed * self._rate)

    @property
    def waiters(self) -> int:
        return self._waiters

    def get_stats(self) -> Dict[str, Any]:
        return {
            "rate":      self._rate,
            "burst":     self._burst,
            "available": round(self.available_tokens, 2),
            "waiters":   self._waiters,
        }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Async Retry
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@dataclass
class RetryConfig:
    """Configuration for retry behavior."""
    max_attempts:       int     = 3
    base_delay:         float   = 1.0
    max_delay:          float   = 60.0
    backoff_factor:     float   = 2.0
    jitter:             bool    = True
    retry_on:           Tuple   = (Exception,)
    no_retry_on:        Tuple   = ()


class AsyncRetry:
    """
    Async retry decorator and context manager.

    Supports:
    ─────────────────────────────────────────────────────
    • Exponential backoff with jitter
    • Configurable retry exceptions
    • Max delay cap
    • Retry callback for logging/monitoring
    • Async and sync function support
    """

    def __init__(self, config: Optional[RetryConfig] = None, **kwargs):
        self._config = config or RetryConfig(**kwargs)

    def __call__(self, func: Callable) -> Callable:
        """Use as decorator."""
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            return await self.execute(func, *args, **kwargs)
        return wrapper

    async def execute(
        self,
        func:       Callable,
        *args:      Any,
        **kwargs:   Any,
    ) -> Any:
        """Execute function with retry logic."""
        config          = self._config
        last_exception  = None

        for attempt in range(config.max_attempts):
            try:
                if asyncio.iscoroutinefunction(func):
                    return await func(*args, **kwargs)
                else:
                    loop = asyncio.get_event_loop()
                    return await loop.run_in_executor(
                        None, functools.partial(func, *args, **kwargs)
                    )

            except config.no_retry_on:
                raise

            except config.retry_on as exc:
                last_exception = exc

                if attempt == config.max_attempts - 1:
                    break

                # Compute delay with exponential backoff
                delay = min(
                    config.base_delay * (config.backoff_factor ** attempt),
                    config.max_delay,
                )
                if config.jitter:
                    delay *= random.uniform(0.5, 1.5)

                logger.debug(
                    f"[AsyncRetry] Attempt {attempt + 1}/{config.max_attempts} "
                    f"failed: {exc}. Retrying in {delay:.1f}s"
                )
                await asyncio.sleep(delay)

        raise last_exception or Exception("Max retries exceeded")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Async Utilities
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class AsyncUtils:
    """
    Collection of async utility functions.

    Features:
    ─────────────────────────────────────────────────────
    • Concurrent task execution with limits
    • Timeout wrappers
    • Async generators
    • Task group management
    • Periodic task runner
    • Async caching
    """

    @staticmethod
    async def run_concurrent(
        coros:          List[Coroutine],
        max_concurrent: int     = 10,
        return_exceptions: bool = True,
    ) -> List[Any]:
        """
        Run coroutines with a concurrency limit.
        Returns results in the same order as input.
        """
        semaphore = asyncio.Semaphore(max_concurrent)

        async def _limited(coro):
            async with semaphore:
                return await coro

        return await asyncio.gather(
            *[_limited(c) for c in coros],
            return_exceptions=return_exceptions,
        )

    @staticmethod
    async def run_with_timeout(
        coro:       Coroutine,
        timeout:    float,
        default:    Any = None,
    ) -> Any:
        """Run coroutine with timeout, returning default on timeout."""
        try:
            return await asyncio.wait_for(coro, timeout=timeout)
        except asyncio.TimeoutError:
            logger.debug(
                f"[AsyncUtils] Timeout after {timeout}s"
            )
            return default

    @staticmethod
    async def retry(
        coro_factory:   Callable[[], Coroutine],
        max_attempts:   int     = 3,
        delay:          float   = 1.0,
        backoff:        float   = 2.0,
    ) -> Any:
        """Simple retry wrapper."""
        last_exc = None
        for attempt in range(max_attempts):
            try:
                return await coro_factory()
            except Exception as exc:
                last_exc    = exc
                if attempt < max_attempts - 1:
                    wait    = delay * (backoff ** attempt)
                    await asyncio.sleep(wait)
        raise last_exc

    @staticmethod
    async def gather_with_limit(
        *coros,
        limit:  int     = 10,
    ) -> List[Any]:
        """Gather coroutines with concurrency limit."""
        sem = asyncio.Semaphore(limit)

        async def _wrap(c):
            async with sem:
                return await c

        return await asyncio.gather(*[_wrap(c) for c in coros])

    @staticmethod
    async def sleep_jitter(
        base_ms:    float,
        jitter_pct: float = 0.3,
    ) -> None:
        """Sleep for base_ms with jitter."""
        jitter      = base_ms * jitter_pct
        actual_ms   = base_ms + random.uniform(-jitter, jitter)
        actual_ms   = max(10, actual_ms)
        await asyncio.sleep(actual_ms / 1000)

    @staticmethod
    async def wait_for_condition(
        condition:  Callable[[], bool],
        timeout:    float       = 30.0,
        check_interval: float   = 0.5,
        message:    str         = "condition",
    ) -> bool:
        """
        Wait until condition() returns True.
        Returns True if condition met, False on timeout.
        """
        start = time.monotonic()
        while time.monotonic() - start < timeout:
            if condition():
                return True
            await asyncio.sleep(check_interval)

        logger.debug(
            f"[AsyncUtils] Timeout waiting for: {message}"
        )
        return False

    @staticmethod
    async def periodic(
        coro_factory:   Callable[[], Coroutine],
        interval:       float,
        run_immediately: bool = False,
    ) -> None:
        """
        Run a coroutine periodically.
        Use as: asyncio.create_task(AsyncUtils.periodic(fn, 5.0))
        """
        if run_immediately:
            await coro_factory()

        while True:
            await asyncio.sleep(interval)
            await coro_factory()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Async Cache
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class AsyncCache:
    """
    Simple async-safe in-memory cache with TTL.

    Usage:
        cache = AsyncCache(ttl=300)
        result = await cache.get("key")
        await cache.set("key", value)
    """

    def __init__(
        self,
        ttl:        float   = 300.0,
        max_size:   int     = 1000,
    ):
        self._ttl       = ttl
        self._max_size  = max_size
        self._store:    Dict[str, Tuple[Any, float]]    = {}
        self._lock      = asyncio.Lock()

    async def get(self, key: str) -> Optional[Any]:
        """Get cached value or None if expired/missing."""
        async with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None

            value, expires_at = entry
            if time.monotonic() > expires_at:
                del self._store[key]
                return None

            return value

    async def set(
        self,
        key:    str,
        value:  Any,
        ttl:    Optional[float] = None,
    ) -> None:
        """Set cached value with TTL."""
        async with self._lock:
            # Evict oldest if full
            if len(self._store) >= self._max_size:
                oldest_key = min(
                    self._store,
                    key=lambda k: self._store[k][1],
                )
                del self._store[oldest_key]

            expires_at = time.monotonic() + (ttl or self._ttl)
            self._store[key] = (value, expires_at)

    async def delete(self, key: str) -> bool:
        """Delete a cached value."""
        async with self._lock:
            if key in self._store:
                del self._store[key]
                return True
            return False

    async def clear(self) -> None:
        """Clear all cached values."""
        async with self._lock:
            self._store.clear()

    async def cleanup_expired(self) -> int:
        """Remove expired entries. Returns count removed."""
        async with self._lock:
            now     = time.monotonic()
            expired = [
                k for k, (_, exp) in self._store.items()
                if now > exp
            ]
            for k in expired:
                del self._store[k]
            return len(expired)

    @property
    def size(self) -> int:
        return len(self._store)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Task Manager
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TaskManager:
    """
    Manages a collection of background asyncio tasks.
    Provides lifecycle control and error handling.
    """

    def __init__(self):
        self._tasks:    Dict[str, asyncio.Task] = {}
        self._errors:   List[Dict]              = []

    def add(
        self,
        name:   str,
        coro:   Coroutine,
    ) -> asyncio.Task:
        """Add and start a named background task."""
        if name in self._tasks:
            existing = self._tasks[name]
            if not existing.done():
                existing.cancel()

        task = asyncio.create_task(coro, name=name)
        task.add_done_callback(
            lambda t: self._on_done(name, t)
        )
        self._tasks[name] = task
        return task

    def _on_done(self, name: str, task: asyncio.Task) -> None:
        """Handle task completion."""
        if task.cancelled():
            logger.debug(f"[TaskManager] Cancelled: {name}")
        elif task.exception():
            exc = task.exception()
            logger.error(f"[TaskManager] Error in '{name}': {exc}")
            self._errors.append({
                "task":      name,
                "error":     str(exc),
                "timestamp": time.time(),
            })

    async def cancel(self, name: str) -> bool:
        """Cancel a named task."""
        task = self._tasks.get(name)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            return True
        return False

    async def cancel_all(self) -> None:
        """Cancel all managed tasks."""
        for name in list(self._tasks.keys()):
            await self.cancel(name)

    async def wait_all(self, timeout: float = 30.0) -> None:
        """Wait for all tasks to complete."""
        active = [
            t for t in self._tasks.values()
            if not t.done()
        ]
        if active:
            await asyncio.wait(active, timeout=timeout)

    def is_running(self, name: str) -> bool:
        task = self._tasks.get(name)
        return task is not None and not task.done()

    def get_status(self) -> Dict[str, str]:
        return {
            name: (
                "running"   if not task.done()    else
                "cancelled" if task.cancelled()   else
                "error"     if task.exception()   else
                "done"
            )
            for name, task in self._tasks.items()
        }

    @property
    def active_count(self) -> int:
        return sum(
            1 for t in self._tasks.values() if not t.done()
        )

    @property
    def error_count(self) -> int:
        return len(self._errors)