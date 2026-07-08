"""
Jubra Traffic Pro - Structured Logging System
Production-grade logging with JSON output, log rotation,
async handlers, and real-time GUI streaming.
"""

import asyncio
import json
import logging
import logging.handlers
import os
import sys
import time
import traceback
import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Optional
from enum import Enum


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Log Entry
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@dataclass
class LogEntry:
    """Structured log entry for streaming and storage."""
    level:      str
    message:    str
    module:     str
    timestamp:  float           = field(default_factory=time.time)
    session_id: Optional[str]   = None
    extra:      Dict[str, Any]  = field(default_factory=dict)
    exc_info:   Optional[str]   = None

    @property
    def iso_timestamp(self) -> str:
        return datetime.fromtimestamp(self.timestamp).isoformat()

    def to_dict(self) -> Dict[str, Any]:
        d = {
            "timestamp":    self.iso_timestamp,
            "level":        self.level,
            "module":       self.module,
            "message":      self.message,
        }
        if self.session_id:
            d["session_id"] = self.session_id
        if self.extra:
            d.update(self.extra)
        if self.exc_info:
            d["exception"] = self.exc_info
        return d

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), default=str)

    def to_colored_string(self) -> str:
        """Colored terminal string."""
        colors = {
            "DEBUG":    "\033[36m",
            "INFO":     "\033[32m",
            "WARNING":  "\033[33m",
            "ERROR":    "\033[31m",
            "CRITICAL": "\033[35m",
        }
        reset  = "\033[0m"
        color  = colors.get(self.level, "")
        ts     = datetime.fromtimestamp(self.timestamp).strftime(
            "%H:%M:%S.%f"
        )[:12]
        module = self.module[:20].ljust(20)
        return (
            f"{color}[{ts}] [{self.level:8s}] "
            f"[{module}] {self.message}{reset}"
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Memory Ring Buffer Handler
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class RingBufferHandler(logging.Handler):
    """
    In-memory circular buffer log handler.
    Used for GUI log viewer and recent-log queries.
    """

    def __init__(self, capacity: int = 2000):
        super().__init__()
        self._buffer:       Deque[LogEntry] = deque(maxlen=capacity)
        self._capacity      = capacity
        self._lock          = threading.Lock()
        self._callbacks:    List[Callable[[LogEntry], None]] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            entry = LogEntry(
                level      = record.levelname,
                message    = self.format(record),
                module     = record.name,
                timestamp  = record.created,
                session_id = getattr(record, "session_id", None),
                extra      = getattr(record, "extra_fields", {}),
                exc_info   = (
                    "".join(traceback.format_exception(*record.exc_info))
                    if record.exc_info else None
                ),
            )
            with self._lock:
                self._buffer.append(entry)

            # Notify registered callbacks (GUI, etc.)
            for cb in self._callbacks:
                try:
                    cb(entry)
                except Exception:
                    pass
        except Exception:
            self.handleError(record)

    def get_entries(
        self,
        count:          int             = 100,
        level_filter:   Optional[str]   = None,
        module_filter:  Optional[str]   = None,
        search:         Optional[str]   = None,
    ) -> List[LogEntry]:
        with self._lock:
            entries = list(self._buffer)

        if level_filter:
            level_order = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
            min_idx     = level_order.index(level_filter.upper()) \
                if level_filter.upper() in level_order else 0
            entries = [
                e for e in entries
                if level_order.index(e.level) >= min_idx
            ]

        if module_filter:
            entries = [
                e for e in entries
                if module_filter.lower() in e.module.lower()
            ]

        if search:
            entries = [
                e for e in entries
                if search.lower() in e.message.lower()
            ]

        return entries[-count:]

    def add_callback(self, callback: Callable[[LogEntry], None]) -> None:
        self._callbacks.append(callback)

    def remove_callback(self, callback: Callable) -> None:
        self._callbacks = [c for c in self._callbacks if c != callback]

    def clear(self) -> None:
        with self._lock:
            self._buffer.clear()

    @property
    def size(self) -> int:
        return len(self._buffer)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# JSON Formatter
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class JSONFormatter(logging.Formatter):
    """Formats log records as JSON lines for structured logging."""

    def __init__(self, include_extra: bool = True):
        super().__init__()
        self._include_extra = include_extra

    def format(self, record: logging.LogRecord) -> str:
        data: Dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created).isoformat(),
            "level":     record.levelname,
            "logger":    record.name,
            "message":   record.getMessage(),
            "module":    record.module,
            "function":  record.funcName,
            "line":      record.lineno,
        }

        if self._include_extra:
            for key, val in record.__dict__.items():
                if key not in (
                    "name", "msg", "args", "levelname", "levelno",
                    "pathname", "filename", "module", "exc_info",
                    "exc_text", "stack_info", "lineno", "funcName",
                    "created", "msecs", "relativeCreated", "thread",
                    "threadName", "processName", "process", "message",
                ):
                    try:
                        json.dumps(val)
                        data[key] = val
                    except (TypeError, ValueError):
                        data[key] = str(val)

        if record.exc_info:
            data["exception"] = self.formatException(record.exc_info)

        return json.dumps(data, default=str)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Colored Console Formatter
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class ColoredFormatter(logging.Formatter):
    """Beautiful colored console output."""

    COLORS = {
        "DEBUG":    "\033[36m",    # Cyan
        "INFO":     "\033[32m",    # Green
        "WARNING":  "\033[33m",    # Yellow
        "ERROR":    "\033[31m",    # Red
        "CRITICAL": "\033[35;1m",  # Bold Magenta
    }
    RESET   = "\033[0m"
    BOLD    = "\033[1m"
    DIM     = "\033[2m"

    def format(self, record: logging.LogRecord) -> str:
        color  = self.COLORS.get(record.levelname, "")
        ts     = datetime.fromtimestamp(record.created).strftime(
            "%Y-%m-%d %H:%M:%S.%f"
        )[:23]
        level  = f"{color}{record.levelname:8s}{self.RESET}"
        name   = f"{self.DIM}{record.name[:30]:30s}{self.RESET}"
        msg    = record.getMessage()

        line = f"{self.DIM}{ts}{self.RESET} {level} {name} {msg}"

        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)

        return line


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Logger Factory
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class LoggerFactory:
    """
    Centralized logger configuration factory.
    Configures all handlers and formatters for the application.
    """

    _ring_buffer: Optional[RingBufferHandler] = None
    _initialized: bool = False

    @classmethod
    def setup(
        cls,
        log_level:      str     = "INFO",
        log_dir:        str     = "logs",
        json_output:    bool    = True,
        console_output: bool    = True,
        file_output:    bool    = True,
        max_file_mb:    int     = 50,
        backup_count:   int     = 10,
        ring_capacity:  int     = 2000,
        quiet_modules:  Optional[List[str]] = None,
    ) -> "RingBufferHandler":
        """
        Set up all logging handlers for the application.
        Returns the ring buffer handler for GUI access.
        """
        if cls._initialized:
            return cls._ring_buffer

        # Create log directory
        log_path = Path(log_dir)
        log_path.mkdir(parents=True, exist_ok=True)

        # Root logger
        root = logging.getLogger()
        root.setLevel(getattr(logging, log_level.upper(), logging.INFO))

        # Remove default handlers
        root.handlers.clear()

        # ── Console Handler ─────────────────────────────
        if console_output:
            console_handler = logging.StreamHandler(sys.stdout)
            console_handler.setFormatter(ColoredFormatter())
            console_handler.setLevel(
                getattr(logging, log_level.upper(), logging.INFO)
            )
            root.addHandler(console_handler)

        # ── Rotating File Handler (plain text) ──────────
        if file_output:
            plain_handler = logging.handlers.RotatingFileHandler(
                filename    = log_path / "bot.log",
                maxBytes    = max_file_mb * 1024 * 1024,
                backupCount = backup_count,
                encoding    = "utf-8",
            )
            plain_handler.setFormatter(logging.Formatter(
                fmt     = "%(asctime)s [%(levelname)8s] %(name)s: %(message)s",
                datefmt = "%Y-%m-%d %H:%M:%S",
            ))
            plain_handler.setLevel(logging.DEBUG)
            root.addHandler(plain_handler)

        # ── JSON Log Handler ────────────────────────────
        if json_output and file_output:
            json_handler = logging.handlers.RotatingFileHandler(
                filename    = log_path / "bot.jsonl",
                maxBytes    = max_file_mb * 1024 * 1024,
                backupCount = backup_count,
                encoding    = "utf-8",
            )
            json_handler.setFormatter(JSONFormatter())
            json_handler.setLevel(logging.DEBUG)
            root.addHandler(json_handler)

        # ── Error-only File Handler ─────────────────────
        if file_output:
            error_handler = logging.handlers.RotatingFileHandler(
                filename    = log_path / "errors.log",
                maxBytes    = 10 * 1024 * 1024,
                backupCount = 5,
                encoding    = "utf-8",
            )
            error_handler.setFormatter(logging.Formatter(
                "%(asctime)s [%(levelname)s] %(name)s:%(lineno)d\n"
                "%(message)s\n"
            ))
            error_handler.setLevel(logging.ERROR)
            root.addHandler(error_handler)

        # ── Ring Buffer Handler (for GUI) ───────────────
        ring_handler = RingBufferHandler(capacity=ring_capacity)
        ring_handler.setFormatter(
            logging.Formatter("%(message)s")
        )
        ring_handler.setLevel(logging.DEBUG)
        root.addHandler(ring_handler)
        cls._ring_buffer = ring_handler

        # ── Quiet noisy third-party modules ────────────
        noisy_defaults = [
            "urllib3", "selenium", "asyncio",
            "aiohttp", "chardet", "charset_normalizer",
        ]
        for mod in (quiet_modules or []) + noisy_defaults:
            logging.getLogger(mod).setLevel(logging.WARNING)

        cls._initialized = True

        logging.getLogger(__name__).info(
            f"Logging initialized: level={log_level}, "
            f"dir={log_dir}, json={json_output}"
        )

        return ring_handler

    @classmethod
    def get_ring_buffer(cls) -> Optional["RingBufferHandler"]:
        return cls._ring_buffer

    @classmethod
    def get_logger(cls, name: str) -> logging.Logger:
        return logging.getLogger(name)

    @classmethod
    def set_level(cls, level: str) -> None:
        logging.getLogger().setLevel(
            getattr(logging, level.upper(), logging.INFO)
        )