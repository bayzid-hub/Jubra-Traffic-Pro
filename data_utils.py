"""
Jubra Traffic Pro - Data Utilities
Common data processing, validation, transformation,
and file handling utilities used across all modules.
"""

import json
import csv
import re
import hashlib
import random
import time
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple, Union
from urllib.parse import urlparse, urljoin, urlunparse
from dataclasses import dataclass

logger = logging.getLogger(__name__)


class DataUtils:
    """
    General-purpose data utilities.

    Features:
    ─────────────────────────────────────────────────────
    • URL validation and normalization
    • Domain extraction and comparison
    • JSON/CSV file loading with error handling
    • Data chunking and batching
    • String sanitization and cleaning
    • IP address validation
    • Weighted random sampling
    • Data fingerprinting
    • Safe dict/list operations
    """

    # ── URL Utilities ──────────────────────────────────────

    @staticmethod
    def normalize_url(url: str) -> str:
        """
        Normalize a URL to a canonical form.
        Removes trailing slashes, lowercases scheme/host.
        """
        if not url:
            return ""
        url = url.strip()
        if not url.startswith(("http://", "https://")):
            url = "https://" + url

        try:
            parsed = urlparse(url)
            # Lowercase scheme and netloc
            normalized = urlunparse((
                parsed.scheme.lower(),
                parsed.netloc.lower(),
                parsed.path.rstrip("/") or "/",
                parsed.params,
                parsed.query,
                "",  # Remove fragment
            ))
            return normalized
        except Exception:
            return url

    @staticmethod
    def extract_domain(url: str) -> str:
        """Extract domain from URL without www prefix."""
        try:
            parsed = urlparse(url)
            domain = parsed.netloc.lower()
            return domain.lstrip("www.")
        except Exception:
            return ""

    @staticmethod
    def is_valid_url(url: str) -> bool:
        """Check if a URL is valid and reachable format."""
        try:
            parsed = urlparse(url)
            return all([
                parsed.scheme in ("http", "https"),
                parsed.netloc,
                len(url) < 2048,
            ])
        except Exception:
            return False

    @staticmethod
    def is_same_domain(url1: str, url2: str) -> bool:
        """Check if two URLs belong to the same domain."""
        return DataUtils.extract_domain(url1) == DataUtils.extract_domain(url2)

    @staticmethod
    def build_url(base: str, path: str = "", params: Optional[Dict] = None) -> str:
        """Build a URL from components."""
        try:
            full = urljoin(base, path)
            if params:
                from urllib.parse import urlencode
                separator = "&" if "?" in full else "?"
                full     += separator + urlencode(params)
            return full
        except Exception:
            return base

    @staticmethod
    def get_url_depth(url: str) -> int:
        """Get the depth of a URL path."""
        try:
            path  = urlparse(url).path
            parts = [p for p in path.split("/") if p]
            return len(parts)
        except Exception:
            return 0

    # ── File Loading ───────────────────────────────────────

    @staticmethod
    def load_json(
        filepath:   str,
        default:    Any = None,
    ) -> Any:
        """Load JSON file with error handling."""
        path = Path(filepath)
        if not path.exists():
            logger.debug(f"[DataUtils] File not found: {filepath}")
            return default

        try:
            content = path.read_text(encoding="utf-8")
            return json.loads(content)
        except json.JSONDecodeError as exc:
            logger.error(f"[DataUtils] JSON parse error {filepath}: {exc}")
            return default
        except Exception as exc:
            logger.error(f"[DataUtils] Load error {filepath}: {exc}")
            return default

    @staticmethod
    def save_json(
        filepath:   str,
        data:       Any,
        indent:     int     = 2,
        ensure_ascii: bool  = False,
    ) -> bool:
        """Save data to JSON file."""
        try:
            path = Path(filepath)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(data, indent=indent, ensure_ascii=ensure_ascii,
                           default=str),
                encoding="utf-8",
            )
            return True
        except Exception as exc:
            logger.error(f"[DataUtils] Save JSON error {filepath}: {exc}")
            return False

    @staticmethod
    def load_lines(
        filepath:   str,
        skip_empty: bool    = True,
        skip_comments: bool = True,
        strip:      bool    = True,
    ) -> List[str]:
        """Load a text file as a list of lines."""
        path = Path(filepath)
        if not path.exists():
            return []

        try:
            lines = path.read_text(encoding="utf-8").splitlines()
            result = []
            for line in lines:
                if strip:
                    line = line.strip()
                if skip_empty and not line:
                    continue
                if skip_comments and line.startswith("#"):
                    continue
                result.append(line)
            return result
        except Exception as exc:
            logger.error(f"[DataUtils] Load lines error: {exc}")
            return []

    @staticmethod
    def load_csv(
        filepath:   str,
        has_header: bool = True,
    ) -> List[Dict[str, str]]:
        """Load CSV file as list of dicts."""
        path = Path(filepath)
        if not path.exists():
            return []

        try:
            rows = []
            with open(path, "r", encoding="utf-8") as f:
                if has_header:
                    reader = csv.DictReader(f)
                    rows   = list(reader)
                else:
                    reader = csv.reader(f)
                    rows   = [
                        {str(i): v for i, v in enumerate(row)}
                        for row in reader
                    ]
            return rows
        except Exception as exc:
            logger.error(f"[DataUtils] CSV load error: {exc}")
            return []

    # ── String Utilities ───────────────────────────────────

    @staticmethod
    def sanitize_string(
        s:              str,
        max_length:     int     = 1000,
        strip_html:     bool    = True,
        strip_newlines: bool    = False,
    ) -> str:
        """Sanitize a string for safe use."""
        if not s:
            return ""

        result = str(s)

        if strip_html:
            result = re.sub(r"<[^>]+>", "", result)

        if strip_newlines:
            result = result.replace("\n", " ").replace("\r", "")

        result = result.strip()

        if len(result) > max_length:
            result = result[:max_length] + "..."

        return result

    @staticmethod
    def slugify(s: str) -> str:
        """Convert string to URL-safe slug."""
        s = s.lower().strip()
        s = re.sub(r"[^\w\s-]", "", s)
        s = re.sub(r"[\s_-]+", "-", s)
        s = re.sub(r"^-+|-+$", "", s)
        return s

    @staticmethod
    def truncate(s: str, max_len: int = 50, suffix: str = "...") -> str:
        """Truncate string to max length."""
        if len(s) <= max_len:
            return s
        return s[:max_len - len(suffix)] + suffix

    @staticmethod
    def mask_sensitive(
        s:              str,
        visible_chars:  int = 4,
        mask_char:      str = "*",
    ) -> str:
        """Mask sensitive string showing only first N chars."""
        if len(s) <= visible_chars:
            return mask_char * len(s)
        return s[:visible_chars] + mask_char * (len(s) - visible_chars)

    # ── IP Utilities ───────────────────────────────────────

    @staticmethod
    def is_valid_ip(ip: str) -> bool:
        """Validate IPv4 or IPv6 address."""
        import ipaddress
        try:
            ipaddress.ip_address(ip)
            return True
        except ValueError:
            return False

    @staticmethod
    def is_private_ip(ip: str) -> bool:
        """Check if IP is in private range."""
        import ipaddress
        try:
            return ipaddress.ip_address(ip).is_private
        except ValueError:
            return False

    @staticmethod
    def ip_to_country_estimate(ip: str) -> str:
        """Very rough IP to country (first octet class only)."""
        try:
            first_octet = int(ip.split(".")[0])
            if first_octet in range(1, 10):
                return "US"
            elif first_octet in range(10, 50):
                return "EU"
            return "UNKNOWN"
        except Exception:
            return "UNKNOWN"

    # ── Data Sampling ──────────────────────────────────────

    @staticmethod
    def weighted_choice(
        items:      List[Any],
        weights:    List[float],
    ) -> Any:
        """Select item with weighted probability."""
        if not items:
            raise ValueError("Items list is empty")

        total       = sum(weights)
        r           = random.uniform(0, total)
        cumulative  = 0.0
        for item, weight in zip(items, weights):
            cumulative += weight
            if r <= cumulative:
                return item
        return items[-1]

    @staticmethod
    def reservoir_sample(
        items:  List[Any],
        k:      int,
    ) -> List[Any]:
        """Random sample of k items without replacement."""
        if k >= len(items):
            return list(items)
        return random.sample(items, k)

    @staticmethod
    def chunk(
        lst:        List[Any],
        chunk_size: int,
    ) -> List[List[Any]]:
        """Split list into chunks of chunk_size."""
        return [
            lst[i:i + chunk_size]
            for i in range(0, len(lst), chunk_size)
        ]

    @staticmethod
    def flatten(nested: List[List[Any]]) -> List[Any]:
        """Flatten one level of nesting."""
        return [item for sublist in nested for item in sublist]

    # ── Fingerprinting ─────────────────────────────────────

    @staticmethod
    def fingerprint(data: Any) -> str:
        """Generate a stable fingerprint for any data."""
        try:
            raw = json.dumps(data, sort_keys=True, default=str)
        except Exception:
            raw = str(data)
        return hashlib.md5(raw.encode()).hexdigest()[:16]

    @staticmethod
    def fingerprint_sha256(data: Any) -> str:
        """Generate SHA-256 fingerprint."""
        try:
            raw = json.dumps(data, sort_keys=True, default=str)
        except Exception:
            raw = str(data)
        return hashlib.sha256(raw.encode()).hexdigest()

    # ── Safe Dict/List Operations ──────────────────────────

    @staticmethod
    def deep_get(
        d:          Dict,
        *keys:      str,
        default:    Any = None,
    ) -> Any:
        """Safely get nested dict value."""
        current = d
        for key in keys:
            if not isinstance(current, dict):
                return default
            current = current.get(key, default)
        return current

    @staticmethod
    def deep_merge(base: Dict, override: Dict) -> Dict:
        """Deep merge two dicts (override wins)."""
        import copy
        result = copy.deepcopy(base)
        for key, val in override.items():
            if (
                key in result and
                isinstance(result[key], dict) and
                isinstance(val, dict)
            ):
                result[key] = DataUtils.deep_merge(result[key], val)
            else:
                result[key] = copy.deepcopy(val)
        return result

    @staticmethod
    def remove_duplicates(
        lst:        List[Any],
        key:        Optional[str] = None,
    ) -> List[Any]:
        """Remove duplicates preserving order."""
        seen:   Set    = set()
        result: List   = []
        for item in lst:
            k = item.get(key) if key and isinstance(item, dict) else item
            if k not in seen:
                seen.add(k)
                result.append(item)
        return result

    @staticmethod
    def safe_int(value: Any, default: int = 0) -> int:
        """Safely convert to int."""
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def safe_float(value: Any, default: float = 0.0) -> float:
        """Safely convert to float."""
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    # ── Time Utilities ─────────────────────────────────────

    @staticmethod
    def format_duration(seconds: float) -> str:
        """Format duration as human-readable string."""
        if seconds < 60:
            return f"{seconds:.1f}s"
        elif seconds < 3600:
            return f"{seconds / 60:.1f}m"
        else:
            return f"{seconds / 3600:.1f}h"

    @staticmethod
    def timestamp_to_iso(ts: float) -> str:
        """Convert Unix timestamp to ISO 8601 string."""
        from datetime import datetime
        return datetime.fromtimestamp(ts).isoformat()

    @staticmethod
    def parse_duration(s: str) -> float:
        """Parse duration string to seconds. e.g. '5m', '2h', '30s'."""
        units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
        s = s.strip().lower()
        if s[-1] in units:
            try:
                return float(s[:-1]) * units[s[-1]]
            except ValueError:
                pass
        try:
            return float(s)
        except ValueError:
            return 0.0