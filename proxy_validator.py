"""
Jubra Traffic Pro - Advanced Proxy Validator
Real-time multi-dimension health scoring, anonymity detection,
speed benchmarking, and threat assessment.
"""

import asyncio
import time
import json
import logging
import ssl
import socket
import random
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import aiohttp

from engines.proxy.proxy_engine import (
    Proxy,
    ProxyStatus,
    ProxyGeoInfo,
    ProxyHealthScore,
)
from core.config_manager import ConfigManager

logger = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Validation Result
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@dataclass
class ValidationResult:
    """Complete result of a proxy validation attempt."""
    proxy_id:           str
    is_alive:           bool
    is_anonymous:       bool
    latency_ms:         float
    download_speed_kbps: float
    real_ip:            str          # IP seen by target
    proxy_ip_leaked:    bool         # True if proxy leaked original IP
    headers_leaked:     List[str]    # Proxy-revealing headers found
    geo:                Optional[ProxyGeoInfo]
    anonymity_level:    str          # "transparent", "anonymous", "elite"
    supports_https:     bool
    supports_http:      bool
    is_banned_google:   bool
    is_banned_cloudflare: bool
    threat_score:       float
    error:              Optional[str]
    checked_at:         float

    @property
    def is_elite(self) -> bool:
        return self.anonymity_level == "elite"

    @property
    def health_score(self) -> float:
        """Compute overall health score from validation results."""
        if not self.is_alive:
            return 0.0
        score = 0.5
        if self.is_anonymous:
            score += 0.2
        if self.is_elite:
            score += 0.1
        if not self.proxy_ip_leaked:
            score += 0.1
        if self.latency_ms < 500:
            score += min(0.1, (500 - self.latency_ms) / 5000)
        score -= self.threat_score * 0.2
        if self.is_banned_google:
            score -= 0.15
        if self.is_banned_cloudflare:
            score -= 0.1
        return max(0.0, min(1.0, score))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "proxy_id":             self.proxy_id,
            "is_alive":             self.is_alive,
            "is_anonymous":         self.is_anonymous,
            "anonymity_level":      self.anonymity_level,
            "latency_ms":           round(self.latency_ms, 2),
            "download_speed_kbps":  round(self.download_speed_kbps, 2),
            "real_ip":              self.real_ip,
            "proxy_ip_leaked":      self.proxy_ip_leaked,
            "headers_leaked":       self.headers_leaked,
            "supports_https":       self.supports_https,
            "supports_http":        self.supports_http,
            "is_banned_google":     self.is_banned_google,
            "is_banned_cloudflare": self.is_banned_cloudflare,
            "threat_score":         round(self.threat_score, 4),
            "health_score":         round(self.health_score, 4),
            "error":                self.error,
            "checked_at":           self.checked_at,
        }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Proxy Validator
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class ProxyValidator:
    """
    Multi-Dimensional Proxy Validator.

    Checks:
    ─────────────────────────────────────────────────────
    1. Basic connectivity (alive check)
    2. HTTPS support
    3. Anonymity level (transparent/anonymous/elite)
    4. IP leak detection
    5. Header leak detection (X-Forwarded-For, Via, etc.)
    6. Speed benchmark (download speed)
    7. Geographic resolution
    8. Google ban detection
    9. Cloudflare ban detection
    10. Threat score assessment
    """

    # Headers that reveal proxy usage
    PROXY_HEADERS = {
        "via", "x-forwarded-for", "x-forwarded-host",
        "x-forwarded-proto", "x-real-ip", "x-proxy-id",
        "proxy-connection", "forwarded", "x-client-ip",
        "x-cluster-client-ip", "true-client-ip",
        "cf-connecting-ip", "fastly-client-ip",
    }

    # Endpoints for anonymity check
    ECHO_ENDPOINTS = [
        "https://httpbin.org/headers",
        "https://httpbin.org/ip",
    ]

    # Speed test file (1MB)
    SPEED_TEST_URL = "https://httpbin.org/bytes/1048576"

    # Ban check pages
    GOOGLE_CHECK_URL = "https://www.google.com/search?q=test"
    CLOUDFLARE_CHECK_URL = "https://www.cloudflare.com"

    def __init__(
        self,
        config:                 Optional[ConfigManager] = None,
        timeout:                float                   = 15.0,
        check_anonymity:        bool                    = True,
        check_speed:            bool                    = True,
        check_geo:              bool                    = True,
        check_bans:             bool                    = False,
        speed_test_size_kb:     int                     = 512,
        max_concurrent:         int                     = 30,
    ):
        self._config            = config
        self._timeout           = timeout
        self._check_anonymity   = check_anonymity
        self._check_speed       = check_speed
        self._check_geo         = check_geo
        self._check_bans        = check_bans
        self._speed_size        = speed_test_size_kb * 1024
        self._semaphore         = asyncio.Semaphore(max_concurrent)
        self._own_ip:   str     = ""

    async def initialize(self) -> None:
        """Detect own real IP for leak comparison."""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    "https://api.ipify.org?format=json",
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    data = await resp.json()
                    self._own_ip = data.get("ip", "")
                    logger.info(
                        f"[ProxyValidator] Own IP detected: {self._own_ip}"
                    )
        except Exception as exc:
            logger.warning(f"[ProxyValidator] Could not detect own IP: {exc}")

    async def validate(self, proxy: Proxy) -> ValidationResult:
        """
        Full validation of a proxy.
        Returns comprehensive ValidationResult.
        """
        async with self._semaphore:
            return await self._run_validation(proxy)

    async def validate_batch(
        self,
        proxies:    List[Proxy],
        concurrent: int = 20,
    ) -> List[ValidationResult]:
        """Validate multiple proxies concurrently."""
        sem = asyncio.Semaphore(concurrent)

        async def _validate_one(p: Proxy) -> ValidationResult:
            async with sem:
                return await self._run_validation(p)

        results = await asyncio.gather(
            *[_validate_one(p) for p in proxies],
            return_exceptions=True,
        )

        valid_results = []
        for r in results:
            if isinstance(r, ValidationResult):
                valid_results.append(r)
            else:
                logger.error(f"[ProxyValidator] Batch error: {r}")

        return valid_results

    async def quick_check(self, proxy: Proxy) -> bool:
        """
        Fast alive-only check. Returns True if proxy responds.
        Much faster than full validation.
        """
        try:
            timeout = aiohttp.ClientTimeout(total=min(self._timeout, 8.0))
            connector = aiohttp.TCPConnector(ssl=False)
            async with aiohttp.ClientSession(
                connector=connector, timeout=timeout
            ) as session:
                start = time.monotonic()
                async with session.get(
                    "https://httpbin.org/ip",
                    proxy=proxy.url,
                ) as resp:
                    if resp.status == 200:
                        latency_ms = (time.monotonic() - start) * 1000
                        proxy.record_success(latency_ms=latency_ms)
                        return True
                    return False
        except Exception:
            proxy.record_failure()
            return False

    # ── Internal Validation ────────────────────────────────

    async def _run_validation(self, proxy: Proxy) -> ValidationResult:
        """Execute all validation checks."""
        result_kwargs = {
            "proxy_id":             proxy.proxy_id,
            "is_alive":             False,
            "is_anonymous":         False,
            "latency_ms":           0.0,
            "download_speed_kbps":  0.0,
            "real_ip":              "",
            "proxy_ip_leaked":      False,
            "headers_leaked":       [],
            "geo":                  None,
            "anonymity_level":      "unknown",
            "supports_https":       False,
            "supports_http":        False,
            "is_banned_google":     False,
            "is_banned_cloudflare": False,
            "threat_score":         0.0,
            "error":                None,
            "checked_at":           time.time(),
        }

        try:
            connector = aiohttp.TCPConnector(ssl=False, limit=5)
            timeout   = aiohttp.ClientTimeout(total=self._timeout)

            async with aiohttp.ClientSession(
                connector=connector,
                timeout=timeout,
            ) as session:

                # 1. Basic connectivity + latency
                alive, latency_ms, real_ip = await self._check_alive(
                    proxy, session
                )
                result_kwargs["is_alive"]   = alive
                result_kwargs["latency_ms"] = latency_ms
                result_kwargs["real_ip"]    = real_ip

                if not alive:
                    result_kwargs["error"] = "Connection failed"
                    proxy.record_failure()
                    await proxy.set_status(ProxyStatus.FAILED)
                    return ValidationResult(**result_kwargs)

                # 2. HTTP/HTTPS support
                result_kwargs["supports_http"]  = True
                result_kwargs["supports_https"] = await self._check_https(
                    proxy, session
                )

                # 3. Anonymity level + header leaks
                if self._check_anonymity:
                    anon_level, headers_leaked, ip_leaked = await self._check_anonymity_level(
                        proxy, session, real_ip
                    )
                    result_kwargs["anonymity_level"]   = anon_level
                    result_kwargs["headers_leaked"]    = headers_leaked
                    result_kwargs["proxy_ip_leaked"]   = ip_leaked
                    result_kwargs["is_anonymous"]      = anon_level in (
                        "anonymous", "elite"
                    )

                # 4. Speed test
                if self._check_speed:
                    speed_kbps = await self._check_speed_test(proxy, session)
                    result_kwargs["download_speed_kbps"] = speed_kbps

                # 5. Geo resolution
                if self._check_geo:
                    geo = await self._resolve_geo(proxy, session)
                    result_kwargs["geo"] = geo
                    if geo:
                        proxy.update_geo(geo)
                        result_kwargs["threat_score"] = geo.threat_score

                # 6. Ban checks (optional, slower)
                if self._check_bans:
                    google_ban, cf_ban = await self._check_bans_async(
                        proxy, session
                    )
                    result_kwargs["is_banned_google"]     = google_ban
                    result_kwargs["is_banned_cloudflare"] = cf_ban

                # Update proxy health
                proxy.record_success(latency_ms=latency_ms)
                await proxy.set_status(ProxyStatus.HEALTHY)
                proxy.last_check_at = time.monotonic()

        except asyncio.TimeoutError:
            result_kwargs["error"] = "Timeout"
            proxy.record_failure(timeout=True)
            await proxy.set_status(ProxyStatus.FAILED)
        except Exception as exc:
            result_kwargs["error"] = str(exc)
            proxy.record_failure()
            await proxy.set_status(ProxyStatus.FAILED)
            logger.debug(f"[ProxyValidator] Error {proxy.proxy_id}: {exc}")

        return ValidationResult(**result_kwargs)

    async def _check_alive(
        self,
        proxy:   Proxy,
        session: aiohttp.ClientSession,
    ) -> Tuple[bool, float, str]:
        """Check basic connectivity. Returns (alive, latency_ms, real_ip)."""
        try:
            start = time.monotonic()
            async with session.get(
                "https://api.ipify.org?format=json",
                proxy=proxy.url,
            ) as resp:
                latency_ms = (time.monotonic() - start) * 1000
                if resp.status == 200:
                    data = await resp.json()
                    real_ip = data.get("ip", "")
                    return True, latency_ms, real_ip
                return False, latency_ms, ""
        except Exception:
            return False, 0.0, ""

    async def _check_https(
        self,
        proxy:   Proxy,
        session: aiohttp.ClientSession,
    ) -> bool:
        """Check HTTPS support through proxy."""
        try:
            async with session.get(
                "https://httpbin.org/ip",
                proxy=proxy.url,
                timeout=aiohttp.ClientTimeout(total=8.0),
            ) as resp:
                return resp.status == 200
        except Exception:
            return False

    async def _check_anonymity_level(
        self,
        proxy:   Proxy,
        session: aiohttp.ClientSession,
        real_ip: str,
    ) -> Tuple[str, List[str], bool]:
        """
        Check anonymity level by inspecting headers.
        Returns (anonymity_level, leaked_headers, ip_leaked)
        """
        leaked_headers = []
        ip_leaked = False

        try:
            async with session.get(
                "https://httpbin.org/headers",
                proxy=proxy.url,
                timeout=aiohttp.ClientTimeout(total=10.0),
            ) as resp:
                if resp.status != 200:
                    return "unknown", [], False

                data = await resp.json()
                headers = {
                    k.lower(): v
                    for k, v in data.get("headers", {}).items()
                }

                # Find proxy-revealing headers
                for header_name in self.PROXY_HEADERS:
                    if header_name in headers:
                        leaked_headers.append(header_name)
                        # Check if our real IP is in the value
                        if self._own_ip and self._own_ip in headers[header_name]:
                            ip_leaked = True
                        if real_ip and real_ip in headers[header_name]:
                            ip_leaked = True

                # Determine anonymity level
                if not leaked_headers:
                    anonymity_level = "elite"
                elif not ip_leaked:
                    anonymity_level = "anonymous"
                else:
                    anonymity_level = "transparent"

                return anonymity_level, leaked_headers, ip_leaked

        except Exception as exc:
            logger.debug(f"[ProxyValidator] Anonymity check error: {exc}")
            return "unknown", [], False

    async def _check_speed_test(
        self,
        proxy:   Proxy,
        session: aiohttp.ClientSession,
    ) -> float:
        """Measure download speed in kbps."""
        try:
            url = f"https://httpbin.org/bytes/{self._speed_size}"
            start = time.monotonic()
            total_bytes = 0

            async with session.get(
                url,
                proxy=proxy.url,
                timeout=aiohttp.ClientTimeout(total=20.0),
            ) as resp:
                async for chunk in resp.content.iter_chunked(8192):
                    total_bytes += len(chunk)
                    # Stop after getting enough data
                    if total_bytes >= self._speed_size:
                        break

            elapsed = time.monotonic() - start
            if elapsed > 0:
                speed_kbps = (total_bytes / 1024) / elapsed
                return round(speed_kbps, 2)
        except Exception:
            pass
        return 0.0

    async def _resolve_geo(
        self,
        proxy:   Proxy,
        session: aiohttp.ClientSession,
    ) -> Optional[ProxyGeoInfo]:
        """Resolve geographic information for proxy IP."""
        geo_apis = [
            f"http://ip-api.com/json/?fields=status,country,countryCode,"
            f"region,regionName,city,zip,lat,lon,timezone,isp,org,as,"
            f"hosting,proxy,query",
            "https://ipapi.co/json/",
        ]

        for api_url in geo_apis:
            try:
                async with session.get(
                    api_url,
                    proxy=proxy.url,
                    timeout=aiohttp.ClientTimeout(total=8.0),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if isinstance(data, dict) and data.get("status") != "fail":
                            return ProxyGeoInfo.from_api_response(data)
            except Exception:
                continue

        return None

    async def _check_bans_async(
        self,
        proxy:   Proxy,
        session: aiohttp.ClientSession,
    ) -> Tuple[bool, bool]:
        """Check if proxy is banned by Google and Cloudflare."""
        google_banned = await self._check_google_ban(proxy, session)
        cf_banned     = await self._check_cloudflare_ban(proxy, session)
        return google_banned, cf_banned

    async def _check_google_ban(
        self,
        proxy:   Proxy,
        session: aiohttp.ClientSession,
    ) -> bool:
        """Check if Google shows CAPTCHA/block for this proxy."""
        try:
            headers = {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/125.0.6422.142 Safari/537.36"
                )
            }
            async with session.get(
                "https://www.google.com/search?q=proxy+test",
                proxy=proxy.url,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=12.0),
                allow_redirects=True,
            ) as resp:
                if resp.status == 429:
                    return True
                if resp.status == 200:
                    text = await resp.text()
                    ban_indicators = [
                        "unusual traffic",
                        "automated queries",
                        "recaptcha",
                        "sorry/index",
                        "ipv4/sorry",
                    ]
                    return any(
                        indicator in text.lower()
                        for indicator in ban_indicators
                    )
                return resp.status not in (200, 301, 302)
        except Exception:
            return False

    async def _check_cloudflare_ban(
        self,
        proxy:   Proxy,
        session: aiohttp.ClientSession,
    ) -> bool:
        """Check if Cloudflare blocks this proxy."""
        try:
            async with session.get(
                "https://www.cloudflare.com",
                proxy=proxy.url,
                timeout=aiohttp.ClientTimeout(total=10.0),
                allow_redirects=True,
            ) as resp:
                if resp.status in (403, 429, 503):
                    return True
                if resp.status == 200:
                    text = await resp.text()
                    return "Access denied" in text or "blocked" in text.lower()
                return False
        except Exception:
            return False

    def filter_by_result(
        self,
        proxies:        List[Proxy],
        results:        List[ValidationResult],
        min_score:      float   = 0.5,
        elite_only:     bool    = False,
        no_google_ban:  bool    = False,
        max_latency_ms: float   = 3000.0,
        min_speed_kbps: float   = 0.0,
    ) -> List[Proxy]:
        """
        Filter proxies based on validation results.
        Returns ordered list of proxies meeting all criteria.
        """
        result_map = {r.proxy_id: r for r in results}
        filtered = []

        for proxy in proxies:
            result = result_map.get(proxy.proxy_id)
            if not result:
                continue
            if not result.is_alive:
                continue
            if result.health_score < min_score:
                continue
            if elite_only and not result.is_elite:
                continue
            if no_google_ban and result.is_banned_google:
                continue
            if result.latency_ms > max_latency_ms:
                continue
            if result.download_speed_kbps < min_speed_kbps:
                continue
            filtered.append((proxy, result.health_score))

        # Sort by health score descending
        filtered.sort(key=lambda x: x[1], reverse=True)
        return [p for p, _ in filtered]