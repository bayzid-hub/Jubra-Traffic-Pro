"""
Jubra Traffic Pro - Intelligent Proxy Rotator
Advanced rotation algorithms with adaptive scoring, campaign-aware
rotation, geo-distribution enforcement, and rotation analytics.
"""

import asyncio
import time
import math
import random
import logging
import hashlib
from dataclasses import dataclass, field
from typing import (
    Any, Dict, List, Optional, Set, Tuple,
    Callable, Deque
)
from collections import defaultdict, deque
from enum import Enum, auto

from engines.proxy.proxy_engine import (
    Proxy,
    ProxyEngine,
    ProxyProtocol,
    ProxyType,
    ProxyStatus,
    RotationStrategy,
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
# Rotation Window Tracker
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class RotationWindowTracker:
    """
    Sliding window tracker for rotation frequency analysis.
    Tracks rotations per proxy per time window.
    Used to enforce cooldown between reuse of same proxy.
    """

    def __init__(
        self,
        window_seconds: float = 300.0,
        max_history:    int   = 10000,
    ):
        self._window    = window_seconds
        self._history:  Deque[Tuple[float, str]] = deque(maxlen=max_history)
        self._by_proxy: Dict[str, Deque[float]]  = defaultdict(
            lambda: deque(maxlen=500)
        )
        self._lock      = asyncio.Lock()

    async def record(self, proxy_id: str) -> None:
        async with self._lock:
            now = time.monotonic()
            self._history.append((now, proxy_id))
            self._by_proxy[proxy_id].append(now)

    async def get_usage_count(
        self,
        proxy_id:       str,
        window_seconds: Optional[float] = None,
    ) -> int:
        """Get how many times proxy was used in the time window."""
        window = window_seconds or self._window
        async with self._lock:
            now = time.monotonic()
            times = self._by_proxy.get(proxy_id, deque())
            return sum(1 for t in times if now - t <= window)

    async def get_recent_proxies(
        self,
        count: int = 10,
    ) -> List[str]:
        """Get list of most recently used proxy IDs."""
        async with self._lock:
            seen: Set[str] = set()
            result = []
            for _, pid in reversed(list(self._history)):
                if pid not in seen:
                    seen.add(pid)
                    result.append(pid)
                if len(result) >= count:
                    break
            return result

    async def get_rotation_rate(self, window_seconds: float = 60.0) -> float:
        """Get rotations per minute in recent window."""
        async with self._lock:
            now = time.monotonic()
            recent = sum(
                1 for ts, _ in self._history
                if now - ts <= window_seconds
            )
            return recent / (window_seconds / 60.0)

    async def is_cooling_down(
        self,
        proxy_id:         str,
        min_reuse_gap_s:  float = 30.0,
    ) -> bool:
        """Check if proxy was used too recently."""
        async with self._lock:
            times = self._by_proxy.get(proxy_id)
            if not times:
                return False
            last_used = times[-1]
            return (time.monotonic() - last_used) < min_reuse_gap_s

    def get_all_stats(self) -> Dict[str, Any]:
        now = time.monotonic()
        return {
            "window_seconds":   self._window,
            "total_recorded":   len(self._history),
            "unique_proxies":   len(self._by_proxy),
            "recent_rate_rpm":  sum(
                1 for ts, _ in self._history
                if now - ts <= 60
            ),
        }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Geo Distribution Enforcer
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class GeoDistributionEnforcer:
    """
    Enforces geographic distribution targets across rotations.
    Ensures traffic appears to come from the configured country mix.
    Uses deficit tracking to prioritize under-represented geos.
    """

    def __init__(
        self,
        target_distribution: Dict[str, float],
        window_size:         int = 1000,
    ):
        # Normalize targets to sum to 1.0
        total = sum(target_distribution.values())
        self._targets: Dict[str, float] = {
            k: v / total
            for k, v in target_distribution.items()
        }
        self._window_size = window_size
        self._history:    Deque[str]  = deque(maxlen=window_size)
        self._counts:     Dict[str, int] = defaultdict(int)
        self._lock        = asyncio.Lock()

    async def record(self, country_code: str) -> None:
        async with self._lock:
            if len(self._history) == self._window_size:
                removed = self._history[0]
                self._counts[removed] = max(0, self._counts[removed] - 1)
            self._history.append(country_code)
            self._counts[country_code] += 1

    async def get_needed_country(
        self,
        available_countries: Set[str],
    ) -> Optional[str]:
        """
        Return the country code most needed to meet distribution target.
        Returns None if no specific country is preferred.
        """
        async with self._lock:
            total = max(len(self._history), 1)
            deficits: Dict[str, float] = {}

            for country, target_ratio in self._targets.items():
                if country not in available_countries:
                    continue
                actual_ratio = self._counts.get(country, 0) / total
                deficit = target_ratio - actual_ratio
                if deficit > 0:
                    deficits[country] = deficit

            if not deficits:
                return None

            # Return country with largest deficit
            return max(deficits, key=lambda c: deficits[c])

    async def get_distribution_stats(self) -> Dict[str, Any]:
        async with self._lock:
            total = max(len(self._history), 1)
            actual: Dict[str, float] = {
                c: count / total
                for c, count in self._counts.items()
            }
            deviation: Dict[str, float] = {}
            for country, target in self._targets.items():
                actual_val = actual.get(country, 0.0)
                deviation[country] = round(actual_val - target, 4)

            return {
                "target":    self._targets,
                "actual":    {k: round(v, 4) for k, v in actual.items()},
                "deviation": deviation,
                "window":    len(self._history),
            }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Adaptive Score Engine
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class AdaptiveScoreEngine:
    """
    Machine-learning-inspired adaptive scoring for proxy selection.

    Uses:
    ─────────────────────────────────────────────────────
    • Exponential moving average of success rates
    • Recency bias (recent failures weight more)
    • Target-specific performance tracking
    • Thompson sampling for exploration vs exploitation
    • Automatic score decay for unused proxies
    """

    def __init__(
        self,
        learning_rate:  float = 0.15,
        decay_rate:     float = 0.001,
        explore_ratio:  float = 0.10,
    ):
        self._learning_rate = learning_rate
        self._decay_rate    = decay_rate
        self._explore_ratio = explore_ratio

        # Per-proxy adaptive scores
        self._scores:       Dict[str, float]          = {}
        self._alpha_counts: Dict[str, int]             = {}  # successes
        self._beta_counts:  Dict[str, int]             = {}  # failures

        # Per-proxy per-target scores
        self._target_scores: Dict[str, Dict[str, float]] = defaultdict(dict)

        # EMA of success rate per proxy
        self._ema_success:  Dict[str, float] = {}
        self._last_update:  Dict[str, float] = {}

        self._lock = asyncio.Lock()

    async def update(
        self,
        proxy_id:   str,
        success:    bool,
        target:     str = "",
        weight:     float = 1.0,
    ) -> None:
        """Update adaptive score after a proxy use."""
        async with self._lock:
            now = time.monotonic()

            # Initialize
            if proxy_id not in self._scores:
                self._scores[proxy_id]       = 0.7
                self._alpha_counts[proxy_id] = 1
                self._beta_counts[proxy_id]  = 1
                self._ema_success[proxy_id]  = 0.7

            # Update Thompson sampling counts
            if success:
                self._alpha_counts[proxy_id] += int(weight)
            else:
                self._beta_counts[proxy_id]  += int(weight)

            # Update EMA
            ema = self._ema_success[proxy_id]
            outcome = 1.0 if success else 0.0
            self._ema_success[proxy_id] = (
                (1 - self._learning_rate) * ema +
                self._learning_rate * outcome
            )

            # Update target-specific score
            if target:
                target_score = self._target_scores[proxy_id].get(target, 0.7)
                self._target_scores[proxy_id][target] = (
                    (1 - self._learning_rate) * target_score +
                    self._learning_rate * outcome
                )

            # Compute combined score
            self._scores[proxy_id] = self._compute_score(proxy_id)
            self._last_update[proxy_id] = now

    async def get_score(
        self,
        proxy_id: str,
        target:   str = "",
    ) -> float:
        """Get adaptive score for a proxy, optionally target-specific."""
        async with self._lock:
            self._apply_decay(proxy_id)

            base = self._scores.get(proxy_id, 0.5)
            if not target:
                return base

            target_score = self._target_scores.get(proxy_id, {}).get(target)
            if target_score is None:
                return base

            # Blend base and target-specific scores
            return 0.6 * target_score + 0.4 * base

    async def thompson_sample(self, proxy_id: str) -> float:
        """
        Thompson sampling: sample from Beta distribution.
        Balances exploration and exploitation.
        """
        async with self._lock:
            alpha = self._alpha_counts.get(proxy_id, 1)
            beta  = self._beta_counts.get(proxy_id, 1)
            # Sample from Beta(alpha, beta)
            try:
                import numpy as np
                return float(np.random.beta(alpha, beta))
            except ImportError:
                # Fallback without numpy
                # Approximate using uniform samples
                samples = [
                    random.betavariate(alpha, beta)
                    for _ in range(5)
                ]
                return sum(samples) / len(samples)

    async def select_by_thompson(
        self,
        proxy_ids: List[str],
    ) -> Optional[str]:
        """Select best proxy using Thompson sampling."""
        if not proxy_ids:
            return None

        # Decide: explore or exploit
        if random.random() < self._explore_ratio:
            return random.choice(proxy_ids)

        # Sample from each proxy's Beta distribution
        samples = {
            pid: await self.thompson_sample(pid)
            for pid in proxy_ids
        }
        return max(samples, key=lambda pid: samples[pid])

    async def get_top_proxies(
        self,
        proxy_ids:  List[str],
        top_n:      int    = 10,
        target:     str    = "",
    ) -> List[str]:
        """Get top N proxies by adaptive score."""
        scored = []
        for pid in proxy_ids:
            score = await self.get_score(pid, target)
            scored.append((pid, score))
        scored.sort(key=lambda x: x[1], reverse=True)
        return [pid for pid, _ in scored[:top_n]]

    def _compute_score(self, proxy_id: str) -> float:
        """Compute combined adaptive score."""
        ema = self._ema_success.get(proxy_id, 0.5)
        alpha = self._alpha_counts.get(proxy_id, 1)
        beta  = self._beta_counts.get(proxy_id, 1)

        # Bayesian estimate: (alpha) / (alpha + beta)
        bayesian = alpha / (alpha + beta)

        # Uncertainty bonus for less-tested proxies
        total = alpha + beta
        uncertainty = 1.0 / math.sqrt(total + 1)

        # Combined: EMA dominant, bayesian secondary
        score = 0.60 * ema + 0.30 * bayesian + 0.10 * uncertainty
        return max(0.0, min(1.0, score))

    def _apply_decay(self, proxy_id: str) -> None:
        """Apply time-based score decay for inactive proxies."""
        last = self._last_update.get(proxy_id)
        if last is None:
            return
        elapsed_hours = (time.monotonic() - last) / 3600
        if elapsed_hours < 1.0:
            return
        decay = self._decay_rate * elapsed_hours
        if proxy_id in self._scores:
            self._scores[proxy_id] = max(
                0.1,
                self._scores[proxy_id] - decay
            )

    def get_all_scores(self) -> Dict[str, float]:
        return dict(self._scores)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Rotation Plan
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@dataclass
class RotationPlan:
    """
    A pre-computed rotation plan for a campaign or session batch.
    Assigns proxies to sessions in advance for balanced distribution.
    """
    plan_id:            str
    session_count:      int
    assignments:        Dict[str, str]      # session_idx → proxy_id
    country_allocation: Dict[str, int]      # country → session count
    created_at:         float               = field(default_factory=time.monotonic)
    strategy_used:      str                 = ""
    geo_enforced:       bool                = False

    @property
    def size(self) -> int:
        return len(self.assignments)

    def get_proxy_id(self, session_index: int) -> Optional[str]:
        return self.assignments.get(str(session_index))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "plan_id":            self.plan_id,
            "session_count":      self.session_count,
            "assigned":           len(self.assignments),
            "country_allocation": self.country_allocation,
            "strategy":           self.strategy_used,
            "geo_enforced":       self.geo_enforced,
            "created_at":         self.created_at,
        }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Rotation Analytics
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class RotationAnalytics:
    """
    Collects and analyzes proxy rotation patterns.
    Used for optimization and anomaly detection.
    """

    def __init__(self, window: int = 5000):
        self._rotations:        Deque[Dict[str, Any]] = deque(maxlen=window)
        self._by_strategy:      Dict[str, int]        = defaultdict(int)
        self._by_country:       Dict[str, int]        = defaultdict(int)
        self._by_proxy_type:    Dict[str, int]        = defaultdict(int)
        self._success_by_proxy: Dict[str, List[bool]] = defaultdict(list)
        self._latency_by_proxy: Dict[str, List[float]] = defaultdict(list)
        self._lock              = asyncio.Lock()

    async def record(
        self,
        proxy_id:   str,
        country:    str,
        proxy_type: str,
        strategy:   str,
        success:    bool,
        latency_ms: float = 0.0,
    ) -> None:
        async with self._lock:
            entry = {
                "proxy_id":   proxy_id,
                "country":    country,
                "type":       proxy_type,
                "strategy":   strategy,
                "success":    success,
                "latency_ms": latency_ms,
                "timestamp":  time.monotonic(),
            }
            self._rotations.append(entry)
            self._by_strategy[strategy]       += 1
            self._by_country[country]         += 1
            self._by_proxy_type[proxy_type]   += 1
            self._success_by_proxy[proxy_id].append(success)
            if latency_ms > 0:
                self._latency_by_proxy[proxy_id].append(latency_ms)

    async def get_proxy_success_rate(self, proxy_id: str) -> float:
        async with self._lock:
            results = self._success_by_proxy.get(proxy_id, [])
            if not results:
                return 1.0
            return sum(results) / len(results)

    async def get_best_proxies(self, top_n: int = 5) -> List[Tuple[str, float]]:
        """Get top proxies by success rate (min 5 uses)."""
        async with self._lock:
            candidates = [
                (pid, results)
                for pid, results in self._success_by_proxy.items()
                if len(results) >= 5
            ]
            scored = [
                (pid, sum(results) / len(results))
                for pid, results in candidates
            ]
            scored.sort(key=lambda x: x[1], reverse=True)
            return scored[:top_n]

    async def get_worst_proxies(self, top_n: int = 5) -> List[Tuple[str, float]]:
        """Get proxies with lowest success rates."""
        async with self._lock:
            candidates = [
                (pid, results)
                for pid, results in self._success_by_proxy.items()
                if len(results) >= 5
            ]
            scored = [
                (pid, sum(results) / len(results))
                for pid, results in candidates
            ]
            scored.sort(key=lambda x: x[1])
            return scored[:top_n]

    async def detect_anomalies(self) -> List[Dict[str, Any]]:
        """Detect unusual rotation patterns."""
        async with self._lock:
            anomalies = []
            now = time.monotonic()

            # Check for proxies with sudden failure spike
            for proxy_id, results in self._success_by_proxy.items():
                if len(results) < 10:
                    continue
                recent   = results[-5:]
                overall  = results[:-5]
                recent_rate  = sum(recent) / len(recent)
                overall_rate = sum(overall) / len(overall)

                if overall_rate > 0.7 and recent_rate < 0.3:
                    anomalies.append({
                        "type":         "sudden_failure_spike",
                        "proxy_id":     proxy_id,
                        "recent_rate":  round(recent_rate, 3),
                        "overall_rate": round(overall_rate, 3),
                        "severity":     "high",
                    })

            # Check for unusually high rotation rate in last 60s
            recent_count = sum(
                1 for r in self._rotations
                if now - r["timestamp"] <= 60
            )
            if recent_count > 120:
                anomalies.append({
                    "type":      "high_rotation_rate",
                    "count_60s": recent_count,
                    "severity":  "medium",
                })

            return anomalies

    def get_summary(self) -> Dict[str, Any]:
        total = len(self._rotations)
        if total == 0:
            return {"total": 0}

        successes = sum(1 for r in self._rotations if r.get("success"))
        latencies = [
            r["latency_ms"]
            for r in self._rotations
            if r.get("latency_ms", 0) > 0
        ]

        return {
            "total_rotations":  total,
            "success_rate":     round(successes / total, 4),
            "by_strategy":      dict(self._by_strategy),
            "by_country":       dict(sorted(
                self._by_country.items(),
                key=lambda x: -x[1],
            )[:10]),
            "by_proxy_type":    dict(self._by_proxy_type),
            "avg_latency_ms":   round(
                sum(latencies) / len(latencies), 2
            ) if latencies else 0,
            "unique_proxies":   len(self._success_by_proxy),
        }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Proxy Rotator
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class ProxyRotator:
    """
    Jubra Traffic Pro - Intelligent Proxy Rotator

    Advanced rotation engine layered on top of ProxyEngine.

    Features:
    ─────────────────────────────────────────────────────
    • 8 base strategies + Thompson sampling + adaptive scoring
    • Geo-distribution enforcement with deficit tracking
    • Per-session sticky proxy binding
    • Campaign-level proxy isolation
    • Minimum reuse gap enforcement
    • Rotation analytics and anomaly detection
    • Adaptive score learning from outcomes
    • Pre-computed rotation plans for campaigns
    • Failure cascade prevention
    • Proxy warm-up period support
    • Rotation rate limiting
    """

    def __init__(
        self,
        proxy_engine:           ProxyEngine,
        config:                 ConfigManager,
        event_bus:              Optional[EventBus] = None,
        strategy:               RotationStrategy   = RotationStrategy.WEIGHTED,
        min_reuse_gap_seconds:  float              = 30.0,
        geo_distribution:       Optional[Dict[str, float]] = None,
        enforce_geo:            bool               = True,
        use_adaptive_scoring:   bool               = True,
        use_thompson_sampling:  bool               = True,
        campaign_isolation:     bool               = True,
        rotation_rate_limit:    float              = 0.0,  # 0 = no limit
        warmup_uses:            int                = 3,
    ):
        self._engine                = proxy_engine
        self._config                = config
        self._event_bus             = event_bus or get_event_bus()
        self._strategy              = strategy
        self._min_reuse_gap         = min_reuse_gap_seconds
        self._enforce_geo           = enforce_geo
        self._use_adaptive          = use_adaptive_scoring
        self._use_thompson          = use_thompson_sampling
        self._campaign_isolation    = campaign_isolation
        self._rotation_rate_limit   = rotation_rate_limit
        self._warmup_uses           = warmup_uses

        # Sub-systems
        self._window_tracker    = RotationWindowTracker(
            window_seconds=300.0
        )
        self._adaptive_engine   = AdaptiveScoreEngine(
            learning_rate=0.15,
            explore_ratio=0.10,
        )
        self._analytics         = RotationAnalytics()

        # Geo enforcer
        effective_geo = geo_distribution or config.get(
            "traffic.geo_distribution",
            {"US": 0.50, "GB": 0.20, "CA": 0.15, "AU": 0.15},
        )
        self._geo_enforcer = GeoDistributionEnforcer(effective_geo)

        # Sticky sessions: session_id → proxy_id
        self._sticky_sessions:   Dict[str, str] = {}
        self._sticky_lock        = asyncio.Lock()

        # Campaign isolation: campaign_id → Set[proxy_id]
        self._campaign_proxies:  Dict[str, Set[str]] = defaultdict(set)
        self._campaign_lock      = asyncio.Lock()

        # Rate limiting
        self._last_rotation:     float = 0.0
        self._rate_lock          = asyncio.Lock()

        # Pre-computed plans
        self._plans:             Dict[str, RotationPlan] = {}

        # Proxy warm-up tracking
        self._warmup_counts:     Dict[str, int] = defaultdict(int)

        logger.info(
            f"[ProxyRotator] Initialized: strategy={strategy.value}, "
            f"geo_enforce={enforce_geo}, adaptive={use_adaptive_scoring}, "
            f"thompson={use_thompson_sampling}"
        )

    # ── Core Rotation ──────────────────────────────────────

    async def get_proxy(
        self,
        session_id:     str,
        target_url:     str                     = "",
        campaign_id:    Optional[str]           = None,
        country:        Optional[str]           = None,
        proxy_type:     Optional[ProxyType]     = None,
        force_rotate:   bool                    = False,
        exclude_ids:    Optional[Set[str]]      = None,
    ) -> Proxy:
        """
        Get the next proxy for a session using intelligent rotation.

        Selection pipeline:
        1. Check sticky session binding
        2. Apply rate limiting
        3. Enforce geo distribution
        4. Filter candidates
        5. Apply adaptive scoring
        6. Thompson sampling (if enabled)
        7. Record rotation
        """
        # Rate limit enforcement
        if self._rotation_rate_limit > 0:
            await self._enforce_rate_limit()

        # Check sticky binding (unless force rotate)
        if not force_rotate and self._strategy == RotationStrategy.STICKY:
            sticky_proxy = await self._get_sticky(session_id)
            if sticky_proxy and sticky_proxy.is_available:
                return sticky_proxy

        # Check campaign isolation
        campaign_allowed: Optional[Set[str]] = None
        if campaign_id and self._campaign_isolation:
            async with self._campaign_lock:
                campaign_allowed = self._campaign_proxies.get(campaign_id)

        # Determine target country
        target_country = country
        if self._enforce_geo and not target_country:
            available_countries = {
                p.country
                for p in self._engine.get_all_proxies()
                if p.is_available and p.country
            }
            target_country = await self._geo_enforcer.get_needed_country(
                available_countries
            )

        # Get candidates from engine
        exclude = exclude_ids or set()

        # Also exclude recently used proxies
        recent = await self._window_tracker.get_recent_proxies(count=3)
        if len(self._engine.get_all_proxies()) > 10:
            exclude.update(recent)

        try:
            proxy = await self._select_proxy(
                session_id      = session_id,
                target_url      = target_url,
                country         = target_country,
                proxy_type      = proxy_type,
                exclude_ids     = exclude,
                campaign_allowed = campaign_allowed,
            )
        except Exception:
            # Fallback: relax all constraints
            proxy = await self._engine.acquire_proxy(
                session_id=session_id,
                country=None,
                proxy_type=None,
                exclude_ids=None,
            )

        # Update sticky binding
        if self._strategy == RotationStrategy.STICKY:
            await self._set_sticky(session_id, proxy)

        # Update campaign mapping
        if campaign_id:
            async with self._campaign_lock:
                self._campaign_proxies[campaign_id].add(proxy.proxy_id)

        # Record rotation
        await self._window_tracker.record(proxy.proxy_id)
        if proxy.country:
            await self._geo_enforcer.record(proxy.country)

        # Emit rotation event
        await self._event_bus.publish_simple(
            EventCategory.PROXY_ROTATED,
            {
                "proxy_id":   proxy.proxy_id,
                "country":    proxy.country,
                "session_id": session_id,
                "strategy":   self._strategy.value,
                "campaign_id": campaign_id,
            },
            priority=EventPriority.LOW,
            session_id=session_id,
        )

        logger.debug(
            f"[ProxyRotator] Rotated: {proxy.proxy_id} "
            f"({proxy.country}) for {session_id[:8]}"
        )

        return proxy

    async def release_proxy(
        self,
        proxy_id:   str,
        session_id: str,
        success:    bool  = True,
        latency_ms: float = 0.0,
        target_url: str   = "",
        banned:     bool  = False,
        captcha:    bool  = False,
    ) -> None:
        """
        Release proxy and update all scoring systems.
        """
        # Update adaptive score
        if self._use_adaptive:
            target = self._extract_domain(target_url)
            await self._adaptive_engine.update(
                proxy_id=proxy_id,
                success=success,
                target=target,
                weight=2.0 if banned else 1.0,
            )

        # Update rotation analytics
        proxy = self._engine.get_proxy(proxy_id)
        if proxy:
            await self._analytics.record(
                proxy_id   = proxy_id,
                country    = proxy.country,
                proxy_type = proxy.proxy_type.value,
                strategy   = self._strategy.value,
                success    = success,
                latency_ms = latency_ms,
            )

        # Release back to engine
        await self._engine.release_proxy(
            proxy_id   = proxy_id,
            session_id = session_id,
            success    = success,
            latency_ms = latency_ms,
            domain     = self._extract_domain(target_url),
            banned     = banned,
            captcha    = captcha,
        )

        # Remove sticky binding on ban
        if banned:
            await self._clear_sticky(session_id)

    async def create_rotation_plan(
        self,
        plan_id:        str,
        session_count:  int,
        campaign_id:    Optional[str] = None,
        country:        Optional[str] = None,
        proxy_type:     Optional[ProxyType] = None,
    ) -> RotationPlan:
        """
        Pre-compute a rotation plan for a batch of sessions.
        Ensures balanced distribution across available proxies.
        """
        available = self._engine.get_available_proxies(
            country=country,
            proxy_type=proxy_type,
        )

        if not available:
            from core.exceptions import ProxyPoolExhaustedError
            raise ProxyPoolExhaustedError(total_proxies=0)

        assignments: Dict[str, str] = {}
        country_allocation: Dict[str, int] = defaultdict(int)

        # Distribute sessions across proxies
        for i in range(session_count):
            # Select proxy with least assignments so far
            proxy_usage: Dict[str, int] = defaultdict(int)
            for pid in assignments.values():
                proxy_usage[pid] += 1

            # Sort available proxies by usage count + health score
            scored = sorted(
                available,
                key=lambda p: (
                    proxy_usage.get(p.proxy_id, 0) * 2.0
                    - p.health.overall * 5.0
                ),
            )

            # Geo-aware selection
            if self._enforce_geo:
                geo_countries = {p.country for p in available if p.country}
                needed_country = await self._geo_enforcer.get_needed_country(
                    geo_countries
                )
                if needed_country:
                    geo_candidates = [
                        p for p in scored
                        if p.country == needed_country
                    ]
                    if geo_candidates:
                        scored = geo_candidates + [
                            p for p in scored
                            if p not in geo_candidates
                        ]

            selected = scored[0]
            assignments[str(i)] = selected.proxy_id
            country_allocation[selected.country] += 1

            # Record for geo distribution tracking
            if selected.country:
                await self._geo_enforcer.record(selected.country)

        plan = RotationPlan(
            plan_id            = plan_id,
            session_count      = session_count,
            assignments        = assignments,
            country_allocation = dict(country_allocation),
            strategy_used      = self._strategy.value,
            geo_enforced       = self._enforce_geo,
        )
        self._plans[plan_id] = plan

        logger.info(
            f"[ProxyRotator] Plan created: {plan_id} | "
            f"{session_count} sessions | "
            f"countries={dict(country_allocation)}"
        )
        return plan

    async def get_plan_proxy(
        self,
        plan_id:        str,
        session_index:  int,
        session_id:     str,
    ) -> Optional[Proxy]:
        """Get the pre-assigned proxy for a session in a rotation plan."""
        plan = self._plans.get(plan_id)
        if not plan:
            logger.warning(f"[ProxyRotator] Plan not found: {plan_id}")
            return None

        proxy_id = plan.get_proxy_id(session_index)
        if not proxy_id:
            return None

        proxy = self._engine.get_proxy(proxy_id)
        if proxy and proxy.is_available:
            proxy.acquire(session_id)
            return proxy

        # Fallback to dynamic rotation
        return await self.get_proxy(session_id=session_id)

    # ── Sticky Session Management ──────────────────────────

    async def _get_sticky(self, session_id: str) -> Optional[Proxy]:
        async with self._sticky_lock:
            proxy_id = self._sticky_sessions.get(session_id)
            if not proxy_id:
                return None
            return self._engine.get_proxy(proxy_id)

    async def _set_sticky(self, session_id: str, proxy: Proxy) -> None:
        async with self._sticky_lock:
            self._sticky_sessions[session_id] = proxy.proxy_id

    async def _clear_sticky(self, session_id: str) -> None:
        async with self._sticky_lock:
            self._sticky_sessions.pop(session_id, None)

    async def clear_sticky_session(self, session_id: str) -> None:
        """Public method to clear sticky binding for a session."""
        await self._clear_sticky(session_id)

    # ── Internal Selection ─────────────────────────────────

    async def _select_proxy(
        self,
        session_id:      str,
        target_url:      str,
        country:         Optional[str],
        proxy_type:      Optional[ProxyType],
        exclude_ids:     Set[str],
        campaign_allowed: Optional[Set[str]],
    ) -> Proxy:
        """
        Internal proxy selection with adaptive scoring.
        """
        # Get candidates from engine
        candidates = self._engine.get_available_proxies(
            country=country,
            proxy_type=proxy_type,
        )

        # Apply exclusions
        candidates = [
            p for p in candidates
            if p.proxy_id not in exclude_ids
        ]

        # Apply campaign filter
        if campaign_allowed is not None:
            campaign_candidates = [
                p for p in candidates
                if p.proxy_id in campaign_allowed
            ]
            if campaign_candidates:
                candidates = campaign_candidates

        # Apply warmup filter (prefer warmed up proxies)
        if self._warmup_uses > 0:
            warmed    = [
                p for p in candidates
                if self._warmup_counts.get(p.proxy_id, 0) >= self._warmup_uses
            ]
            unwarmed  = [
                p for p in candidates
                if p not in warmed
            ]
            # Use mostly warmed, occasionally unwarmed for warming
            if warmed and len(candidates) > self._warmup_uses:
                if random.random() < 0.85:
                    candidates = warmed
                else:
                    candidates = unwarmed or warmed

        if not candidates:
            # Fallback to engine acquire with relaxed constraints
            return await self._engine.acquire_proxy(
                session_id=session_id,
                country=None,
                exclude_ids=exclude_ids,
            )

        # Apply adaptive scoring + Thompson sampling
        if self._use_adaptive and self._use_thompson:
            proxy_ids = [p.proxy_id for p in candidates]
            target_domain = self._extract_domain(target_url)
            selected_id = await self._adaptive_engine.select_by_thompson(
                proxy_ids
            )
            if selected_id:
                selected = next(
                    (p for p in candidates if p.proxy_id == selected_id),
                    None,
                )
                if selected and selected.is_available:
                    selected.acquire(session_id)
                    self._warmup_counts[selected.proxy_id] += 1
                    return selected

        # Apply adaptive scoring without Thompson
        if self._use_adaptive:
            proxy_ids = [p.proxy_id for p in candidates]
            target_domain = self._extract_domain(target_url)
            top_ids = await self._adaptive_engine.get_top_proxies(
                proxy_ids,
                top_n=max(3, len(proxy_ids) // 3),
                target=target_domain,
            )
            top_candidates = [
                p for p in candidates
                if p.proxy_id in set(top_ids)
            ]
            if top_candidates:
                candidates = top_candidates

        # Final selection by base strategy
        selected = self._apply_base_strategy(candidates, session_id)
        selected.acquire(session_id)
        self._warmup_counts[selected.proxy_id] += 1
        return selected

    def _apply_base_strategy(
        self,
        candidates: List[Proxy],
        session_id: str,
    ) -> Proxy:
        """Apply the base rotation strategy to candidate list."""
        if not candidates:
            raise ValueError("No candidates")

        if self._strategy == RotationStrategy.RANDOM:
            return random.choice(candidates)

        elif self._strategy == RotationStrategy.ROUND_ROBIN:
            return candidates[int(time.monotonic() * 1000) % len(candidates)]

        elif self._strategy == RotationStrategy.LEAST_USED:
            return min(candidates, key=lambda p: p.health.total_requests)

        elif self._strategy == RotationStrategy.LEAST_FAILED:
            return min(candidates, key=lambda p: p.health.failed)

        elif self._strategy == RotationStrategy.PERFORMANCE:
            return max(candidates, key=lambda p: p.health.overall)

        elif self._strategy == RotationStrategy.GEO_OPTIMAL:
            type_score = {
                ProxyType.RESIDENTIAL: 1.0,
                ProxyType.ISP:         0.8,
                ProxyType.MOBILE:      0.8,
                ProxyType.DATACENTER:  0.5,
                ProxyType.TOR:         0.3,
            }
            return max(
                candidates,
                key=lambda p: (
                    type_score.get(p.proxy_type, 0.5) * 0.5 +
                    p.health.overall * 0.5
                ),
            )

        else:  # WEIGHTED (default)
            total_weight = sum(
                p.weight * p.health.overall
                for p in candidates
            )
            if total_weight <= 0:
                return random.choice(candidates)

            r = random.uniform(0, total_weight)
            cumulative = 0.0
            for proxy in candidates:
                cumulative += proxy.weight * proxy.health.overall
                if r <= cumulative:
                    return proxy
            return candidates[-1]

    # ── Rate Limiting ──────────────────────────────────────

    async def _enforce_rate_limit(self) -> None:
        """Enforce minimum gap between rotations."""
        if self._rotation_rate_limit <= 0:
            return
        async with self._rate_lock:
            now = time.monotonic()
            min_gap = 1.0 / self._rotation_rate_limit
            elapsed = now - self._last_rotation
            if elapsed < min_gap:
                await asyncio.sleep(min_gap - elapsed)
            self._last_rotation = time.monotonic()

    # ── Utilities ──────────────────────────────────────────

    @staticmethod
    def _extract_domain(url: str) -> str:
        """Extract domain from URL for target-specific scoring."""
        if not url:
            return ""
        try:
            from urllib.parse import urlparse
            parsed = urlparse(url)
            return parsed.netloc or ""
        except Exception:
            return ""

    # ── Query / Analytics ──────────────────────────────────

    async def get_analytics(self) -> Dict[str, Any]:
        """Get comprehensive rotation analytics."""
        summary = self._analytics.get_summary()
        anomalies = await self._analytics.detect_anomalies()
        geo_stats = await self._geo_enforcer.get_distribution_stats()
        window_stats = self._window_tracker.get_all_stats()
        adaptive_scores = self._adaptive_engine.get_all_scores()

        best_proxies  = await self._analytics.get_best_proxies(5)
        worst_proxies = await self._analytics.get_worst_proxies(5)

        return {
            "rotation_summary":  summary,
            "anomalies":         anomalies,
            "geo_distribution":  geo_stats,
            "window_tracker":    window_stats,
            "adaptive_scores":   {
                k: round(v, 4)
                for k, v in list(adaptive_scores.items())[:20]
            },
            "best_proxies":      best_proxies,
            "worst_proxies":     worst_proxies,
            "active_plans":      len(self._plans),
            "sticky_sessions":   len(self._sticky_sessions),
            "campaign_count":    len(self._campaign_proxies),
        }

    async def get_proxy_report(self, proxy_id: str) -> Dict[str, Any]:
        """Get detailed report for a specific proxy."""
        proxy = self._engine.get_proxy(proxy_id)
        if not proxy:
            return {"error": "Proxy not found"}

        adaptive_score = await self._adaptive_engine.get_score(proxy_id)
        success_rate   = await self._analytics.get_proxy_success_rate(proxy_id)
        usage_count    = await self._window_tracker.get_usage_count(proxy_id)
        is_cooling     = await self._window_tracker.is_cooling_down(
            proxy_id,
            self._min_reuse_gap,
        )

        return {
            "proxy":            proxy.to_dict(),
            "adaptive_score":   round(adaptive_score, 4),
            "rotation_success": round(success_rate, 4),
            "usage_last_5min":  usage_count,
            "is_cooling_down":  is_cooling,
            "warmup_uses":      self._warmup_counts.get(proxy_id, 0),
            "is_sticky_for":    [
                sid for sid, pid in self._sticky_sessions.items()
                if pid == proxy_id
            ][:10],
        }

    def set_strategy(self, strategy: RotationStrategy) -> None:
        """Hot-swap rotation strategy."""
        old = self._strategy
        self._strategy = strategy
        logger.info(
            f"[ProxyRotator] Strategy changed: "
            f"{old.value} → {strategy.value}"
        )

    def get_strategy(self) -> RotationStrategy:
        return self._strategy