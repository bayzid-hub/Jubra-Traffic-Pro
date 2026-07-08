"""
Jubra Traffic Pro - Performance Monitor
Real-time system and application performance tracking
with sampling, profiling, and bottleneck detection.
"""

import asyncio
import time
import os
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Callable
from collections import deque
from contextlib import asynccontextmanager

try:
    import psutil
    HAS_PSUTIL = True
except ImportError:
    HAS_PSUTIL = False

from monitoring.metrics_collector import MetricsCollector, MetricType
from core.config_manager import ConfigManager

logger = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Performance Sample
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@dataclass
class PerformanceSample:
    """A point-in-time performance snapshot."""
    timestamp:      float
    cpu_pct:        float
    memory_pct:     float
    memory_mb:      float
    disk_pct:       float
    net_sent_kb:    float
    net_recv_kb:    float
    process_count:  int
    thread_count:   int
    open_files:     int
    event_loop_lag_ms: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "timestamp":    self.timestamp,
            "cpu_pct":      round(self.cpu_pct, 2),
            "memory_pct":   round(self.memory_pct, 2),
            "memory_mb":    round(self.memory_mb, 1),
            "disk_pct":     round(self.disk_pct, 2),
            "net_sent_kb":  round(self.net_sent_kb, 2),
            "net_recv_kb":  round(self.net_recv_kb, 2),
            "process_count": self.process_count,
            "thread_count": self.thread_count,
            "loop_lag_ms":  round(self.event_loop_lag_ms, 2),
        }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Performance Monitor
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class PerformanceMonitor:
    """
    Real-Time Performance Monitor.

    Tracks:
    ─────────────────────────────────────────────────────
    • CPU usage (per-core and total)
    • Memory (RSS, virtual, available)
    • Disk I/O and usage
    • Network I/O (bytes sent/received)
    • Process and thread counts
    • Open file descriptors
    • Asyncio event loop lag
    • Per-operation timing (context manager)
    • Bottleneck detection and reporting
    • Resource leak detection
    """

    def __init__(
        self,
        config:             ConfigManager,
        metrics_collector:  Optional[MetricsCollector]  = None,
        sample_interval:    float                       = 5.0,
        history_size:       int                         = 720,     # 1 hour at 5s
        bottleneck_threshold_cpu: float                 = 80.0,
        bottleneck_threshold_mem: float                 = 75.0,
        loop_lag_threshold_ms:    float                 = 100.0,
    ):
        self._config            = config
        self._metrics           = metrics_collector
        self._sample_interval   = sample_interval
        self._history:          deque = deque(maxlen=history_size)
        self._cpu_threshold     = bottleneck_threshold_cpu
        self._mem_threshold     = bottleneck_threshold_mem
        self._lag_threshold     = loop_lag_threshold_ms

        # Network baseline
        self._last_net_bytes:   Optional[tuple]     = None
        self._last_net_time:    float               = time.monotonic()

        # Operation timing
        self._op_times:         Dict[str, deque]    = {}
        self._op_counts:        Dict[str, int]      = {}

        # Bottleneck tracking
        self._bottlenecks:      deque               = deque(maxlen=100)
        self._bottleneck_count: int                 = 0

        # Background task
        self._sample_task:      Optional[asyncio.Task]  = None
        self._lag_task:         Optional[asyncio.Task]  = None
        self._running:          bool                    = False

        if not HAS_PSUTIL:
            logger.warning(
                "[PerformanceMonitor] psutil not installed, "
                "system metrics unavailable"
            )

        logger.info(
            f"[PerformanceMonitor] Initialized: "
            f"interval={sample_interval}s"
        )

    # ── Lifecycle ──────────────────────────────────────────

    async def start(self) -> None:
        """Start performance monitoring."""
        self._running = True

        self._sample_task = asyncio.create_task(
            self._sampling_loop(),
            name="PerformanceMonitor-Sampler",
        )
        self._lag_task = asyncio.create_task(
            self._loop_lag_monitor(),
            name="PerformanceMonitor-LoopLag",
        )

        # Register with MetricsCollector if available
        if self._metrics:
            self._metrics.register_collector(
                "system",
                self._get_system_metrics,
            )

        logger.info("[PerformanceMonitor] Started")

    async def stop(self) -> None:
        """Stop performance monitoring."""
        self._running = False

        for task in [self._sample_task, self._lag_task]:
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        logger.info("[PerformanceMonitor] Stopped")

    # ── Sampling ───────────────────────────────────────────

    def _take_sample(self) -> Optional[PerformanceSample]:
        """Take a system performance snapshot."""
        if not HAS_PSUTIL:
            return None

        try:
            cpu_pct     = psutil.cpu_percent(interval=None)
            memory      = psutil.virtual_memory()
            disk        = psutil.disk_usage("/")

            # Network delta
            net         = psutil.net_io_counters()
            now         = time.monotonic()
            net_sent_kb = 0.0
            net_recv_kb = 0.0

            if self._last_net_bytes:
                elapsed = max(now - self._last_net_time, 0.001)
                last_sent, last_recv = self._last_net_bytes
                net_sent_kb = (net.bytes_sent - last_sent) / 1024 / elapsed
                net_recv_kb = (net.bytes_recv - last_recv) / 1024 / elapsed

            self._last_net_bytes = (net.bytes_sent, net.bytes_recv)
            self._last_net_time  = now

            # Process info
            proc            = psutil.Process(os.getpid())
            thread_count    = proc.num_threads()
            open_files      = 0
            try:
                open_files  = len(proc.open_files())
            except Exception:
                pass

            return PerformanceSample(
                timestamp       = time.time(),
                cpu_pct         = cpu_pct,
                memory_pct      = memory.percent,
                memory_mb       = memory.used / 1024 / 1024,
                disk_pct        = disk.percent,
                net_sent_kb     = max(0, net_sent_kb),
                net_recv_kb     = max(0, net_recv_kb),
                process_count   = len(psutil.pids()),
                thread_count    = thread_count,
                open_files      = open_files,
            )

        except Exception as exc:
            logger.debug(f"[PerformanceMonitor] Sample error: {exc}")
            return None

    async def _sampling_loop(self) -> None:
        """Background sampling loop."""
        while self._running:
            try:
                await asyncio.sleep(self._sample_interval)

                loop    = asyncio.get_event_loop()
                sample  = await loop.run_in_executor(
                    None, self._take_sample
                )

                if sample:
                    self._history.append(sample)
                    await self._analyze_sample(sample)

                    # Push to metrics collector
                    if self._metrics:
                        await self._metrics.gauge(
                            "system.cpu_pct", sample.cpu_pct
                        )
                        await self._metrics.gauge(
                            "system.memory_pct", sample.memory_pct
                        )
                        await self._metrics.gauge(
                            "system.memory_mb", sample.memory_mb
                        )
                        await self._metrics.gauge(
                            "system.net_recv_kbps", sample.net_recv_kb
                        )

            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error(
                    f"[PerformanceMonitor] Sampling error: {exc}"
                )

    async def _loop_lag_monitor(self) -> None:
        """Monitor asyncio event loop lag."""
        while self._running:
            try:
                start   = time.monotonic()
                await asyncio.sleep(0.1)
                lag_ms  = (time.monotonic() - start - 0.1) * 1000

                if self._history:
                    self._history[-1].event_loop_lag_ms = lag_ms

                if lag_ms > self._lag_threshold:
                    logger.warning(
                        f"[PerformanceMonitor] High event loop lag: "
                        f"{lag_ms:.1f}ms"
                    )

                if self._metrics:
                    await self._metrics.gauge(
                        "system.loop_lag_ms", lag_ms
                    )

                await asyncio.sleep(4.9)

            except asyncio.CancelledError:
                break
            except Exception:
                await asyncio.sleep(5.0)

    async def _analyze_sample(self, sample: PerformanceSample) -> None:
        """Detect bottlenecks in a sample."""
        bottlenecks = []

        if sample.cpu_pct > self._cpu_threshold:
            bottlenecks.append(f"CPU={sample.cpu_pct:.1f}%")

        if sample.memory_pct > self._mem_threshold:
            bottlenecks.append(f"MEM={sample.memory_pct:.1f}%")

        if bottlenecks:
            entry = {
                "timestamp":    sample.timestamp,
                "bottlenecks":  bottlenecks,
                "cpu":          sample.cpu_pct,
                "memory":       sample.memory_pct,
            }
            self._bottlenecks.append(entry)
            self._bottleneck_count += 1

            logger.warning(
                f"[PerformanceMonitor] Bottleneck: "
                f"{', '.join(bottlenecks)}"
            )

    # ── Operation Timing ───────────────────────────────────

    @asynccontextmanager
    async def measure(self, operation_name: str):
        """
        Context manager for measuring operation duration.

        Usage:
            async with monitor.measure("proxy_acquire"):
                proxy = await proxy_engine.acquire_proxy(...)
        """
        start = time.monotonic()
        try:
            yield
        finally:
            elapsed_ms = (time.monotonic() - start) * 1000

            if operation_name not in self._op_times:
                self._op_times[operation_name]  = deque(maxlen=200)
                self._op_counts[operation_name] = 0

            self._op_times[operation_name].append(elapsed_ms)
            self._op_counts[operation_name] += 1

            if self._metrics:
                await self._metrics.histogram(
                    f"op.{operation_name}.duration_ms",
                    elapsed_ms,
                )

    def get_operation_stats(self) -> Dict[str, Dict[str, float]]:
        """Get timing statistics for all measured operations."""
        stats = {}
        for op_name, times in self._op_times.items():
            if not times:
                continue
            sorted_t    = sorted(times)
            n           = len(sorted_t)
            stats[op_name] = {
                "count":    self._op_counts.get(op_name, 0),
                "avg_ms":   round(sum(sorted_t) / n, 2),
                "min_ms":   round(sorted_t[0], 2),
                "max_ms":   round(sorted_t[-1], 2),
                "p95_ms":   round(sorted_t[int(n * 0.95)], 2),
                "p99_ms":   round(sorted_t[int(n * 0.99)], 2),
            }
        return stats

    # ── Query ──────────────────────────────────────────────

    def get_latest_sample(self) -> Optional[PerformanceSample]:
        """Get the most recent performance sample."""
        if self._history:
            return self._history[-1]
        return None

    def get_history(
        self,
        last_n: int = 60,
    ) -> List[PerformanceSample]:
        """Get last N performance samples."""
        return list(self._history)[-last_n:]

    def get_averages(self, last_n: int = 12) -> Dict[str, float]:
        """Get average metrics over last N samples."""
        samples = list(self._history)[-last_n:]
        if not samples:
            return {}

        return {
            "avg_cpu_pct":      round(
                sum(s.cpu_pct for s in samples) / len(samples), 2
            ),
            "avg_memory_pct":   round(
                sum(s.memory_pct for s in samples) / len(samples), 2
            ),
            "avg_memory_mb":    round(
                sum(s.memory_mb for s in samples) / len(samples), 1
            ),
            "avg_net_recv_kbps": round(
                sum(s.net_recv_kb for s in samples) / len(samples), 2
            ),
            "avg_loop_lag_ms":  round(
                sum(s.event_loop_lag_ms for s in samples) / len(samples), 2
            ),
        }

    def get_bottleneck_report(self) -> Dict[str, Any]:
        """Get bottleneck detection report."""
        return {
            "total_bottlenecks":    self._bottleneck_count,
            "recent_bottlenecks":   list(self._bottlenecks)[-10:],
            "operation_stats":      self.get_operation_stats(),
        }

    async def _get_system_metrics(self) -> Dict[str, float]:
        """Metrics for MetricsCollector auto-collection."""
        sample = self.get_latest_sample()
        if not sample:
            return {}
        return {
            "cpu_pct":      sample.cpu_pct,
            "memory_pct":   sample.memory_pct,
            "memory_mb":    sample.memory_mb,
            "disk_pct":     sample.disk_pct,
            "thread_count": sample.thread_count,
            "loop_lag_ms":  sample.event_loop_lag_ms,
        }

    def get_summary(self) -> Dict[str, Any]:
        """Get performance summary."""
        latest  = self.get_latest_sample()
        avgs    = self.get_averages()

        return {
            "latest":           latest.to_dict() if latest else {},
            "averages":         avgs,
            "bottlenecks":      self._bottleneck_count,
            "samples_stored":   len(self._history),
            "operations":       self.get_operation_stats(),
        }