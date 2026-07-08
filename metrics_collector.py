"""
Jubra Traffic Pro - Metrics Collector
Real-time metrics aggregation, time-series storage,
alerting, and export for all subsystems.
"""

import asyncio
import time
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple
from collections import defaultdict, deque
from enum import Enum, auto
from pathlib import Path

from core.event_bus import EventBus, EventCategory, EventPriority, get_event_bus
from core.config_manager import ConfigManager

logger = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Metric Types
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class MetricType(Enum):
    COUNTER     = "counter"     # Monotonically increasing
    GAUGE       = "gauge"       # Can go up or down
    HISTOGRAM   = "histogram"   # Distribution of values
    RATE        = "rate"        # Events per second/minute


@dataclass
class MetricPoint:
    """A single metric data point."""
    name:       str
    value:      float
    metric_type: MetricType     = MetricType.GAUGE
    tags:       Dict[str, str]  = field(default_factory=dict)
    timestamp:  float           = field(default_factory=time.time)
    unit:       str             = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name":      self.name,
            "value":     round(self.value, 4),
            "type":      self.metric_type.value,
            "tags":      self.tags,
            "timestamp": self.timestamp,
            "unit":      self.unit,
        }


@dataclass
class AlertRule:
    """A metric alerting rule."""
    name:           str
    metric_name:    str
    condition:      str         # "gt", "lt", "eq", "gte", "lte"
    threshold:      float
    duration_s:     float       = 0.0   # How long condition must persist
    cooldown_s:     float       = 300.0 # Seconds between repeated alerts
    severity:       str         = "warning"
    message:        str         = ""
    enabled:        bool        = True
    last_fired:     float       = 0.0
    triggered_at:   float       = 0.0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Time Series Store
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TimeSeriesStore:
    """
    In-memory time series storage with automatic pruning.
    Stores metric history for charting and analysis.
    """

    def __init__(
        self,
        max_points_per_metric:  int     = 1000,
        retention_seconds:      float   = 86400.0,  # 24 hours
    ):
        self._series:       Dict[str, deque]    = defaultdict(
            lambda: deque(maxlen=max_points_per_metric)
        )
        self._max_points    = max_points_per_metric
        self._retention     = retention_seconds
        self._lock          = asyncio.Lock()

    async def record(self, point: MetricPoint) -> None:
        """Record a metric data point."""
        async with self._lock:
            self._series[point.name].append(point)

    async def get_series(
        self,
        metric_name:    str,
        start_time:     Optional[float] = None,
        end_time:       Optional[float] = None,
        max_points:     Optional[int]   = None,
    ) -> List[MetricPoint]:
        """Get time series data for a metric."""
        async with self._lock:
            points = list(self._series.get(metric_name, []))

        if start_time:
            points = [p for p in points if p.timestamp >= start_time]
        if end_time:
            points = [p for p in points if p.timestamp <= end_time]
        if max_points:
            points = points[-max_points:]

        return points

    async def get_latest(self, metric_name: str) -> Optional[MetricPoint]:
        """Get the most recent value for a metric."""
        async with self._lock:
            series = self._series.get(metric_name)
            if series:
                return series[-1]
        return None

    async def get_all_metric_names(self) -> List[str]:
        async with self._lock:
            return list(self._series.keys())

    async def prune_old(self) -> int:
        """Remove data points older than retention period."""
        cutoff  = time.time() - self._retention
        removed = 0
        async with self._lock:
            for name, series in self._series.items():
                original = len(series)
                # Filter old points
                fresh = deque(
                    (p for p in series if p.timestamp > cutoff),
                    maxlen=self._max_points,
                )
                self._series[name] = fresh
                removed += original - len(fresh)
        return removed

    async def get_stats(self, metric_name: str) -> Dict[str, float]:
        """Get statistical summary for a metric."""
        points = await self.get_series(metric_name)
        if not points:
            return {}

        values = [p.value for p in points]
        sorted_v = sorted(values)
        n = len(values)

        return {
            "count":    n,
            "min":      round(min(values), 4),
            "max":      round(max(values), 4),
            "avg":      round(sum(values) / n, 4),
            "median":   round(sorted_v[n // 2], 4),
            "p95":      round(sorted_v[int(n * 0.95)], 4),
            "p99":      round(sorted_v[int(n * 0.99)], 4),
            "latest":   round(values[-1], 4),
        }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Metrics Collector
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class MetricsCollector:
    """
    Jubra Traffic Pro - Real-Time Metrics Collector

    Features:
    ─────────────────────────────────────────────────────
    • Counter, Gauge, Histogram, Rate metric types
    • Time-series storage with configurable retention
    • Alert rules with threshold + duration + cooldown
    • EventBus integration for metric broadcasting
    • Metric aggregation (sum, avg, min, max, p95, p99)
    • Auto-collection from registered components
    • CSV/JSON export
    • Dashboard data formatting
    • Metric tagging for multi-dimensional analysis
    """

    def __init__(
        self,
        config:             ConfigManager,
        event_bus:          Optional[EventBus]  = None,
        collection_interval: float             = 5.0,
        retention_seconds:  float              = 86400.0,
        max_points:         int                = 1000,
        enable_export:      bool               = False,
        export_path:        str                = "logs/metrics",
    ):
        self._config            = config
        self._event_bus         = event_bus or get_event_bus()
        self._collection_interval = collection_interval
        self._enable_export     = enable_export
        self._export_path       = Path(export_path)

        # Storage
        self._store             = TimeSeriesStore(max_points, retention_seconds)

        # Counters (monotonic)
        self._counters:         Dict[str, float]    = defaultdict(float)

        # Alert rules
        self._alert_rules:      List[AlertRule]     = []
        self._alert_callbacks:  List[Callable]      = []
        self._alert_history:    deque               = deque(maxlen=500)

        # Component collectors
        self._collectors:       Dict[str, Callable] = {}

        # Background tasks
        self._collect_task:     Optional[asyncio.Task] = None
        self._prune_task:       Optional[asyncio.Task] = None
        self._running:          bool = False

        # Built-in alert rules
        self._setup_default_alerts()

        logger.info(
            f"[MetricsCollector] Initialized: "
            f"interval={collection_interval}s, "
            f"retention={retention_seconds/3600:.1f}h"
        )

    # ── Lifecycle ──────────────────────────────────────────

    async def start(self) -> None:
        """Start metric collection."""
        self._running = True

        self._collect_task = asyncio.create_task(
            self._collection_loop(),
            name="MetricsCollector-Collect",
        )
        self._prune_task = asyncio.create_task(
            self._prune_loop(),
            name="MetricsCollector-Prune",
        )

        logger.info("[MetricsCollector] Started")

    async def stop(self) -> None:
        """Stop metric collection."""
        self._running = False

        for task in [self._collect_task, self._prune_task]:
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        logger.info("[MetricsCollector] Stopped")

    # ── Recording ──────────────────────────────────────────

    async def record(
        self,
        name:           str,
        value:          float,
        metric_type:    MetricType              = MetricType.GAUGE,
        tags:           Optional[Dict[str, str]] = None,
        unit:           str                     = "",
    ) -> None:
        """Record a single metric value."""
        point = MetricPoint(
            name        = name,
            value       = value,
            metric_type = metric_type,
            tags        = tags or {},
            unit        = unit,
        )
        await self._store.record(point)

        # Check alert rules
        await self._check_alerts(point)

        # Broadcast via EventBus (throttled)
        if metric_type != MetricType.HISTOGRAM:
            await self._event_bus.publish_simple(
                EventCategory.METRICS_UPDATE,
                {
                    "metric":   name,
                    "value":    round(value, 4),
                    "type":     metric_type.value,
                    "tags":     tags or {},
                },
                priority=EventPriority.BACKGROUND,
            )

    async def increment(
        self,
        name:   str,
        amount: float               = 1.0,
        tags:   Optional[Dict]      = None,
    ) -> float:
        """Increment a counter metric."""
        self._counters[name] += amount
        await self.record(
            name        = name,
            value       = self._counters[name],
            metric_type = MetricType.COUNTER,
            tags        = tags,
        )
        return self._counters[name]

    async def gauge(
        self,
        name:   str,
        value:  float,
        tags:   Optional[Dict] = None,
        unit:   str            = "",
    ) -> None:
        """Set a gauge metric."""
        await self.record(
            name        = name,
            value       = value,
            metric_type = MetricType.GAUGE,
            tags        = tags,
            unit        = unit,
        )

    async def histogram(
        self,
        name:   str,
        value:  float,
        tags:   Optional[Dict] = None,
        unit:   str            = "ms",
    ) -> None:
        """Record a histogram sample."""
        await self.record(
            name        = name,
            value       = value,
            metric_type = MetricType.HISTOGRAM,
            tags        = tags,
            unit        = unit,
        )

    async def record_many(self, points: List[MetricPoint]) -> None:
        """Record multiple metric points at once."""
        for point in points:
            await self._store.record(point)

    # ── Component Registration ─────────────────────────────

    def register_collector(
        self,
        name:       str,
        collector:  Callable,
    ) -> None:
        """
        Register a component metric collector function.
        Called automatically every collection interval.

        collector() should return Dict[str, float]
        """
        self._collectors[name] = collector
        logger.debug(f"[MetricsCollector] Registered: {name}")

    def unregister_collector(self, name: str) -> None:
        self._collectors.pop(name, None)

    # ── Alert Rules ────────────────────────────────────────

    def add_alert_rule(self, rule: AlertRule) -> None:
        """Add a metric alerting rule."""
        self._alert_rules.append(rule)
        logger.info(
            f"[MetricsCollector] Alert rule: {rule.name} | "
            f"{rule.metric_name} {rule.condition} {rule.threshold}"
        )

    def add_alert_callback(self, callback: Callable) -> None:
        """Register alert notification callback."""
        self._alert_callbacks.append(callback)

    def _setup_default_alerts(self) -> None:
        """Set up built-in alert rules."""
        default_rules = [
            AlertRule(
                name        = "high_detection_rate",
                metric_name = "sessions.detection_rate",
                condition   = "gt",
                threshold   = 0.20,
                duration_s  = 60.0,
                severity    = "critical",
                message     = "Bot detection rate > 20%",
            ),
            AlertRule(
                name        = "low_success_rate",
                metric_name = "sessions.success_rate",
                condition   = "lt",
                threshold   = 0.50,
                duration_s  = 120.0,
                severity    = "warning",
                message     = "Session success rate < 50%",
            ),
            AlertRule(
                name        = "proxy_pool_low",
                metric_name = "proxy.available_count",
                condition   = "lt",
                threshold   = 5.0,
                severity    = "warning",
                message     = "Proxy pool below minimum",
            ),
            AlertRule(
                name        = "high_memory",
                metric_name = "system.memory_pct",
                condition   = "gt",
                threshold   = 85.0,
                severity    = "critical",
                message     = "System memory > 85%",
            ),
            AlertRule(
                name        = "browser_crash_rate",
                metric_name = "browser.crash_rate",
                condition   = "gt",
                threshold   = 0.10,
                severity    = "warning",
                message     = "Browser crash rate > 10%",
            ),
        ]
        for rule in default_rules:
            self._alert_rules.append(rule)

    async def _check_alerts(self, point: MetricPoint) -> None:
        """Check all alert rules against a new metric point."""
        now = time.monotonic()

        for rule in self._alert_rules:
            if not rule.enabled:
                continue
            if rule.metric_name != point.name:
                continue

            # Evaluate condition
            triggered = self._evaluate_condition(
                point.value, rule.condition, rule.threshold
            )

            if triggered:
                if rule.triggered_at == 0:
                    rule.triggered_at = now

                # Check duration condition
                duration_met = (
                    rule.duration_s == 0 or
                    (now - rule.triggered_at) >= rule.duration_s
                )

                # Check cooldown
                cooldown_ok = (now - rule.last_fired) >= rule.cooldown_s

                if duration_met and cooldown_ok:
                    await self._fire_alert(rule, point)
                    rule.last_fired = now
            else:
                rule.triggered_at = 0.0

    @staticmethod
    def _evaluate_condition(
        value:      float,
        condition:  str,
        threshold:  float,
    ) -> bool:
        """Evaluate alert condition."""
        ops = {
            "gt":  value > threshold,
            "lt":  value < threshold,
            "gte": value >= threshold,
            "lte": value <= threshold,
            "eq":  abs(value - threshold) < 0.001,
        }
        return ops.get(condition, False)

    async def _fire_alert(
        self,
        rule:   AlertRule,
        point:  MetricPoint,
    ) -> None:
        """Fire an alert."""
        alert = {
            "rule":     rule.name,
            "metric":   point.name,
            "value":    round(point.value, 4),
            "threshold": rule.threshold,
            "severity": rule.severity,
            "message":  rule.message,
            "timestamp": time.time(),
        }
        self._alert_history.append(alert)

        logger.warning(
            f"[MetricsCollector] ALERT [{rule.severity.upper()}]: "
            f"{rule.message} | value={point.value:.3f}"
        )

        # Notify callbacks
        for cb in self._alert_callbacks:
            try:
                if asyncio.iscoroutinefunction(cb):
                    await cb(alert)
                else:
                    cb(alert)
            except Exception as exc:
                logger.error(f"[MetricsCollector] Alert callback error: {exc}")

        # Publish to EventBus
        await self._event_bus.publish_simple(
            EventCategory.ALERT_TRIGGERED,
            alert,
            priority=EventPriority.HIGH,
        )

    # ── Query ──────────────────────────────────────────────

    async def get_latest(self, metric_name: str) -> Optional[float]:
        """Get the latest value for a metric."""
        point = await self._store.get_latest(metric_name)
        return point.value if point else None

    async def get_series(
        self,
        metric_name:    str,
        minutes:        int = 60,
    ) -> List[Tuple[float, float]]:
        """Get (timestamp, value) pairs for the last N minutes."""
        start   = time.time() - (minutes * 60)
        points  = await self._store.get_series(
            metric_name,
            start_time=start,
        )
        return [(p.timestamp, p.value) for p in points]

    async def get_stats(self, metric_name: str) -> Dict[str, float]:
        """Get statistical summary for a metric."""
        return await self._store.get_stats(metric_name)

    async def get_all_metrics(self) -> Dict[str, float]:
        """Get latest value for all tracked metrics."""
        names   = await self._store.get_all_metric_names()
        result  = {}
        for name in names:
            point = await self._store.get_latest(name)
            if point:
                result[name] = point.value
        return result

    def get_alert_history(self, count: int = 20) -> List[Dict]:
        """Get recent alert history."""
        return list(self._alert_history)[-count:]

    # ── Dashboard Data ─────────────────────────────────────

    async def get_dashboard_data(self) -> Dict[str, Any]:
        """Get all metrics formatted for dashboard display."""
        all_metrics = await self.get_all_metrics()

        return {
            "timestamp":        time.time(),
            "metrics":          all_metrics,
            "alerts":           self.get_alert_history(10),
            "collectors":       list(self._collectors.keys()),
            "alert_rules":      len(self._alert_rules),
        }

    # ── Export ─────────────────────────────────────────────

    async def export_csv(
        self,
        metric_name:    str,
        filepath:       str,
        minutes:        int = 60,
    ) -> bool:
        """Export metric time series to CSV."""
        try:
            series  = await self.get_series(metric_name, minutes)
            path    = Path(filepath)
            path.parent.mkdir(parents=True, exist_ok=True)

            with open(path, "w") as f:
                f.write("timestamp,value\n")
                for ts, val in series:
                    f.write(f"{ts},{val}\n")

            logger.info(
                f"[MetricsCollector] Exported: {metric_name} → {filepath}"
            )
            return True
        except Exception as exc:
            logger.error(f"[MetricsCollector] Export error: {exc}")
            return False

    async def export_json(self, filepath: str) -> bool:
        """Export all current metrics to JSON."""
        try:
            data    = await self.get_dashboard_data()
            path    = Path(filepath)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(data, indent=2, default=str)
            )
            return True
        except Exception as exc:
            logger.error(f"[MetricsCollector] JSON export error: {exc}")
            return False

    # ── Background Tasks ───────────────────────────────────

    async def _collection_loop(self) -> None:
        """Automatically collect metrics from registered components."""
        while self._running:
            try:
                await asyncio.sleep(self._collection_interval)

                for name, collector in self._collectors.items():
                    try:
                        if asyncio.iscoroutinefunction(collector):
                            metrics = await collector()
                        else:
                            metrics = collector()

                        if isinstance(metrics, dict):
                            for metric_name, value in metrics.items():
                                if isinstance(value, (int, float)):
                                    await self.gauge(
                                        f"{name}.{metric_name}",
                                        float(value),
                                    )
                    except Exception as exc:
                        logger.debug(
                            f"[MetricsCollector] Collector error "
                            f"'{name}': {exc}"
                        )

                # Export if enabled
                if self._enable_export:
                    self._export_path.mkdir(parents=True, exist_ok=True)
                    await self.export_json(
                        str(self._export_path / "current.json")
                    )

            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error(
                    f"[MetricsCollector] Collection loop error: {exc}"
                )

    async def _prune_loop(self) -> None:
        """Periodically prune old metric data."""
        while self._running:
            try:
                await asyncio.sleep(3600.0)  # Every hour
                removed = await self._store.prune_old()
                if removed > 0:
                    logger.debug(
                        f"[MetricsCollector] Pruned {removed} old points"
                    )
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error(
                    f"[MetricsCollector] Prune loop error: {exc}"
                )