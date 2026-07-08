"""
Jubra Traffic Pro - Custom Exception Hierarchy
Complete exception system for granular error handling across all modules.
"""

import traceback
import datetime
import json
import hashlib
from typing import Optional, Dict, Any, List
from enum import IntEnum


class Severity(IntEnum):
    """Exception severity levels for monitoring and alerting."""
    DEBUG = 10
    INFO = 20
    WARNING = 30
    ERROR = 40
    CRITICAL = 50
    FATAL = 60


class ErrorCategory:
    """Categorization of errors for analytics and self-healing."""
    NETWORK = "network"
    BROWSER = "browser"
    PROXY = "proxy"
    FINGERPRINT = "fingerprint"
    SESSION = "session"
    CONFIG = "config"
    CAPTCHA = "captcha"
    DETECTION = "detection"
    ENCRYPTION = "encryption"
    ANALYTICS = "analytics"
    BEHAVIOR = "behavior"
    TRAFFIC = "traffic"
    SYSTEM = "system"
    GUI = "gui"
    UNKNOWN = "unknown"


class ErrorContext:
    """
    Rich error context container.
    Captures full execution context when an exception occurs.
    """

    def __init__(
        self,
        module: str = "",
        operation: str = "",
        session_id: Optional[str] = None,
        proxy_id: Optional[str] = None,
        browser_id: Optional[str] = None,
        target_url: Optional[str] = None,
        attempt_number: int = 0,
        max_retries: int = 0,
        metadata: Optional[Dict[str, Any]] = None,
    ):
        self.module = module
        self.operation = operation
        self.session_id = session_id
        self.proxy_id = proxy_id
        self.browser_id = browser_id
        self.target_url = target_url
        self.attempt_number = attempt_number
        self.max_retries = max_retries
        self.metadata = metadata or {}
        self.timestamp = datetime.datetime.utcnow().isoformat()
        self.stack_trace = traceback.format_stack()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "module": self.module,
            "operation": self.operation,
            "session_id": self.session_id,
            "proxy_id": self.proxy_id,
            "browser_id": self.browser_id,
            "target_url": self.target_url,
            "attempt_number": self.attempt_number,
            "max_retries": self.max_retries,
            "metadata": self.metadata,
            "timestamp": self.timestamp,
        }

    def __repr__(self) -> str:
        return (
            f"ErrorContext(module={self.module!r}, "
            f"operation={self.operation!r}, "
            f"session={self.session_id!r})"
        )


class BotException(Exception):
    """
    Base exception for all Jubra Traffic Pro errors.
    
    Features:
    - Severity levels for monitoring
    - Error categorization for analytics
    - Rich context capture
    - Error fingerprinting for deduplication
    - Retry guidance
    - Chain tracking
    """

    default_severity: Severity = Severity.ERROR
    default_category: str = ErrorCategory.UNKNOWN
    default_retryable: bool = False
    default_retry_delay: float = 1.0
    default_max_retries: int = 3

    def __init__(
        self,
        message: str = "",
        severity: Optional[Severity] = None,
        category: Optional[str] = None,
        context: Optional[ErrorContext] = None,
        retryable: Optional[bool] = None,
        retry_delay: Optional[float] = None,
        max_retries: Optional[int] = None,
        cause: Optional[Exception] = None,
        suggestions: Optional[List[str]] = None,
        error_code: Optional[str] = None,
    ):
        super().__init__(message)
        self.bot_message = message
        self.severity = severity or self.default_severity
        self.category = category or self.default_category
        self.context = context or ErrorContext()
        self.retryable = retryable if retryable is not None else self.default_retryable
        self.retry_delay = retry_delay or self.default_retry_delay
        self.max_retries = max_retries or self.default_max_retries
        self.cause = cause
        self.suggestions = suggestions or []
        self.error_code = error_code or self._generate_error_code()
        self.timestamp = datetime.datetime.utcnow().isoformat()
        self.fingerprint = self._generate_fingerprint()
        self._chain: List["BotException"] = []

        if cause and isinstance(cause, BotException):
            self._chain = cause._chain.copy()
            self._chain.append(cause)

    def _generate_error_code(self) -> str:
        """Generate a unique error code based on class hierarchy."""
        class_name = self.__class__.__name__
        category_prefix = self.category[:3].upper()
        severity_prefix = str(self.severity.value)
        return f"JTP-{category_prefix}-{severity_prefix}-{class_name}"

    def _generate_fingerprint(self) -> str:
        """Generate error fingerprint for deduplication."""
        fingerprint_data = (
            f"{self.__class__.__name__}:"
            f"{self.category}:"
            f"{self.context.module}:"
            f"{self.context.operation}:"
            f"{self.bot_message}"
        )
        return hashlib.md5(fingerprint_data.encode()).hexdigest()[:12]

    def add_suggestion(self, suggestion: str) -> "BotException":
        """Add a recovery suggestion. Returns self for chaining."""
        self.suggestions.append(suggestion)
        return self

    def with_context(self, **kwargs) -> "BotException":
        """Update context fields. Returns self for chaining."""
        for key, value in kwargs.items():
            if hasattr(self.context, key):
                setattr(self.context, key, value)
            else:
                self.context.metadata[key] = value
        return self

    @property
    def is_retryable(self) -> bool:
        """Check if this error should be retried."""
        if not self.retryable:
            return False
        if self.context.attempt_number >= self.max_retries:
            return False
        return True

    @property
    def next_retry_delay(self) -> float:
        """Calculate next retry delay with exponential backoff."""
        attempt = self.context.attempt_number
        return self.retry_delay * (2 ** attempt)

    @property
    def error_chain(self) -> List["BotException"]:
        """Get the full chain of errors that led to this one."""
        return self._chain.copy()

    def to_dict(self) -> Dict[str, Any]:
        """Serialize exception to dictionary for logging/monitoring."""
        result = {
            "error_code": self.error_code,
            "fingerprint": self.fingerprint,
            "type": self.__class__.__name__,
            "message": self.bot_message,
            "severity": self.severity.name,
            "severity_value": self.severity.value,
            "category": self.category,
            "retryable": self.retryable,
            "retry_delay": self.retry_delay,
            "max_retries": self.max_retries,
            "timestamp": self.timestamp,
            "context": self.context.to_dict(),
            "suggestions": self.suggestions,
            "chain_length": len(self._chain),
        }
        if self.cause:
            result["cause"] = {
                "type": type(self.cause).__name__,
                "message": str(self.cause),
            }
        return result

    def to_json(self) -> str:
        """Serialize to JSON string."""
        return json.dumps(self.to_dict(), indent=2, default=str)

    def __str__(self) -> str:
        parts = [f"[{self.error_code}] {self.bot_message}"]
        if self.context.module:
            parts.append(f" | Module: {self.context.module}")
        if self.context.operation:
            parts.append(f" | Op: {self.context.operation}")
        if self.context.session_id:
            parts.append(f" | Session: {self.context.session_id}")
        if self.suggestions:
            parts.append(f" | Suggestions: {'; '.join(self.suggestions)}")
        return "".join(parts)

    def __repr__(self) -> str:
        return (
            f"{self.__class__.__name__}("
            f"message={self.bot_message!r}, "
            f"severity={self.severity.name}, "
            f"category={self.category!r}, "
            f"retryable={self.retryable})"
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Configuration Errors
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class ConfigError(BotException):
    """Base configuration error."""
    default_severity = Severity.ERROR
    default_category = ErrorCategory.CONFIG
    default_retryable = False


class ConfigFileNotFoundError(ConfigError):
    """Configuration file not found."""
    default_severity = Severity.CRITICAL

    def __init__(self, filepath: str, **kwargs):
        super().__init__(
            message=f"Configuration file not found: {filepath}",
            suggestions=[
                f"Create config file at: {filepath}",
                "Copy default_config.yaml to config/ directory",
                "Run setup.py to generate default configuration",
            ],
            **kwargs,
        )


class ConfigValidationError(ConfigError):
    """Configuration validation failed."""

    def __init__(self, field: str, value: Any, expected: str, **kwargs):
        super().__init__(
            message=f"Config validation failed for '{field}': got {value!r}, expected {expected}",
            suggestions=[
                f"Set '{field}' to a valid value matching: {expected}",
                "Check default_config.yaml for valid examples",
            ],
            **kwargs,
        )
        self.field = field
        self.invalid_value = value
        self.expected = expected


class ConfigEncryptionError(ConfigError):
    """Configuration encryption/decryption error."""
    default_severity = Severity.CRITICAL

    def __init__(self, operation: str = "decrypt", **kwargs):
        super().__init__(
            message=f"Failed to {operation} configuration data",
            suggestions=[
                "Check encryption key is correct",
                "Regenerate encryption key if lost",
                "Restore config from backup",
            ],
            **kwargs,
        )


class ConfigHotReloadError(ConfigError):
    """Configuration hot-reload failed."""
    default_severity = Severity.WARNING
    default_retryable = True
    default_retry_delay = 5.0

    def __init__(self, reason: str = "", **kwargs):
        super().__init__(
            message=f"Config hot-reload failed: {reason}",
            suggestions=["Check file permissions", "Validate config syntax"],
            **kwargs,
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Proxy Errors
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class ProxyError(BotException):
    """Base proxy error."""
    default_severity = Severity.WARNING
    default_category = ErrorCategory.PROXY
    default_retryable = True
    default_retry_delay = 2.0


class ProxyConnectionError(ProxyError):
    """Failed to connect through proxy."""

    def __init__(self, proxy_address: str, reason: str = "", **kwargs):
        super().__init__(
            message=f"Proxy connection failed: {proxy_address} - {reason}",
            suggestions=[
                "Check proxy is online and accepting connections",
                "Verify proxy credentials",
                "Try a different proxy",
            ],
            **kwargs,
        )
        self.proxy_address = proxy_address


class ProxyAuthenticationError(ProxyError):
    """Proxy authentication failed."""
    default_retryable = False

    def __init__(self, proxy_address: str, **kwargs):
        super().__init__(
            message=f"Proxy authentication failed: {proxy_address}",
            suggestions=[
                "Verify proxy username and password",
                "Check if proxy subscription is active",
                "Contact proxy provider",
            ],
            **kwargs,
        )


class ProxyPoolExhaustedError(ProxyError):
    """All proxies in the pool are unusable."""
    default_severity = Severity.CRITICAL
    default_retryable = True
    default_retry_delay = 30.0

    def __init__(self, total_proxies: int = 0, **kwargs):
        super().__init__(
            message=f"Proxy pool exhausted: all {total_proxies} proxies failed",
            suggestions=[
                "Add more proxies to the pool",
                "Wait for failed proxies to recover",
                "Check network connectivity",
                "Reduce concurrent session count",
            ],
            **kwargs,
        )


class ProxyBannedError(ProxyError):
    """Proxy IP has been banned by target."""
    default_retryable = False

    def __init__(self, proxy_address: str, target: str = "", **kwargs):
        super().__init__(
            message=f"Proxy banned: {proxy_address} blocked by {target}",
            suggestions=[
                "Remove proxy from active pool",
                "Wait before reusing this proxy",
                "Use residential proxies for better success",
            ],
            **kwargs,
        )


class TorCircuitError(ProxyError):
    """Tor circuit creation/renewal error."""
    default_retry_delay = 10.0

    def __init__(self, reason: str = "", **kwargs):
        super().__init__(
            message=f"Tor circuit error: {reason}",
            suggestions=[
                "Check Tor service is running",
                "Verify Tor control port configuration",
                "Try renewing the circuit",
            ],
            **kwargs,
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Browser Errors
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class BrowserError(BotException):
    """Base browser error."""
    default_severity = Severity.ERROR
    default_category = ErrorCategory.BROWSER
    default_retryable = True
    default_retry_delay = 3.0


class BrowserLaunchError(BrowserError):
    """Failed to launch browser instance."""
    default_severity = Severity.CRITICAL

    def __init__(self, reason: str = "", **kwargs):
        super().__init__(
            message=f"Browser launch failed: {reason}",
            suggestions=[
                "Check Chrome/Chromium is installed",
                "Verify chromedriver version matches Chrome",
                "Check system memory availability",
                "Kill zombie browser processes",
            ],
            **kwargs,
        )


class BrowserCrashedError(BrowserError):
    """Browser instance crashed."""

    def __init__(self, browser_id: str = "", reason: str = "", **kwargs):
        super().__init__(
            message=f"Browser crashed (ID: {browser_id}): {reason}",
            suggestions=[
                "Restart browser instance",
                "Reduce browser memory usage",
                "Disable GPU acceleration",
                "Check for resource leaks",
            ],
            **kwargs,
        )


class BrowserPoolExhaustedError(BrowserError):
    """No available browser instances in the pool."""
    default_severity = Severity.CRITICAL
    default_retry_delay = 10.0

    def __init__(self, pool_size: int = 0, active: int = 0, **kwargs):
        super().__init__(
            message=f"Browser pool exhausted: {active}/{pool_size} active",
            suggestions=[
                "Increase browser pool size",
                "Wait for sessions to complete",
                "Reduce concurrent traffic",
            ],
            **kwargs,
        )


class PageLoadError(BrowserError):
    """Page failed to load."""

    def __init__(self, url: str = "", status_code: int = 0, **kwargs):
        super().__init__(
            message=f"Page load failed: {url} (status: {status_code})",
            suggestions=[
                "Check target URL is accessible",
                "Verify proxy connectivity",
                "Increase page load timeout",
            ],
            **kwargs,
        )
        self.url = url
        self.status_code = status_code


class CDPInjectionError(BrowserError):
    """Chrome DevTools Protocol injection failed."""
    default_retryable = False

    def __init__(self, command: str = "", reason: str = "", **kwargs):
        super().__init__(
            message=f"CDP injection failed for '{command}': {reason}",
            suggestions=[
                "Check CDP command syntax",
                "Verify browser supports the CDP domain",
                "Update Chrome to latest version",
            ],
            **kwargs,
        )


class MobileEmulationError(BrowserError):
    """Mobile device emulation error."""

    def __init__(self, device: str = "", reason: str = "", **kwargs):
        super().__init__(
            message=f"Mobile emulation failed for '{device}': {reason}",
            suggestions=[
                "Check device profile is valid",
                "Use a supported device name",
                "Update device metrics database",
            ],
            **kwargs,
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Fingerprint Errors
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class FingerprintError(BotException):
    """Base fingerprint error."""
    default_severity = Severity.ERROR
    default_category = ErrorCategory.FINGERPRINT
    default_retryable = True


class CanvasSpoofError(FingerprintError):
    """Canvas fingerprint spoofing failed."""

    def __init__(self, reason: str = "", **kwargs):
        super().__init__(
            message=f"Canvas spoof failed: {reason}",
            suggestions=[
                "Update canvas noise algorithm",
                "Check WebGL context availability",
                "Regenerate canvas fingerprint profile",
            ],
            **kwargs,
        )


class AudioSpoofError(FingerprintError):
    """Audio fingerprint spoofing failed."""

    def __init__(self, reason: str = "", **kwargs):
        super().__init__(
            message=f"Audio spoof failed: {reason}",
            suggestions=[
                "Check AudioContext API support",
                "Update audio fingerprint noise parameters",
            ],
            **kwargs,
        )


class TLSSpoofError(FingerprintError):
    """TLS/JA3 fingerprint spoofing failed."""
    default_severity = Severity.CRITICAL

    def __init__(self, reason: str = "", **kwargs):
        super().__init__(
            message=f"TLS/JA3 spoof failed: {reason}",
            suggestions=[
                "Update TLS cipher suite configuration",
                "Check browser TLS version support",
                "Use a different browser profile",
            ],
            **kwargs,
        )


class FingerprintConsistencyError(FingerprintError):
    """Fingerprint components are inconsistent."""
    default_severity = Severity.CRITICAL
    default_retryable = False

    def __init__(self, inconsistencies: Optional[List[str]] = None, **kwargs):
        issues = inconsistencies or []
        super().__init__(
            message=f"Fingerprint inconsistency detected: {', '.join(issues)}",
            suggestions=[
                "Regenerate complete fingerprint profile",
                "Ensure all components use same base profile",
                "Check mutation engine consistency rules",
            ],
            **kwargs,
        )
        self.inconsistencies = issues


class MutationError(FingerprintError):
    """Fingerprint mutation engine error."""

    def __init__(self, component: str = "", reason: str = "", **kwargs):
        super().__init__(
            message=f"Mutation failed for '{component}': {reason}",
            suggestions=[
                "Reset mutation state",
                "Check mutation bounds configuration",
            ],
            **kwargs,
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Session Errors
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class SessionError(BotException):
    """Base session error."""
    default_severity = Severity.ERROR
    default_category = ErrorCategory.SESSION
    default_retryable = True


class SessionCreationError(SessionError):
    """Failed to create a new session."""
    default_severity = Severity.CRITICAL

    def __init__(self, reason: str = "", **kwargs):
        super().__init__(
            message=f"Session creation failed: {reason}",
            suggestions=[
                "Check system resources",
                "Verify proxy availability",
                "Reduce concurrent session limit",
            ],
            **kwargs,
        )


class SessionExpiredError(SessionError):
    """Session has expired."""
    default_retryable = False

    def __init__(self, session_id: str = "", ttl: float = 0, **kwargs):
        super().__init__(
            message=f"Session expired: {session_id} (TTL: {ttl}s)",
            suggestions=["Create a new session", "Increase session TTL"],
            **kwargs,
        )


class SessionLimitError(SessionError):
    """Maximum concurrent sessions reached."""
    default_retry_delay = 15.0

    def __init__(self, current: int = 0, maximum: int = 0, **kwargs):
        super().__init__(
            message=f"Session limit reached: {current}/{maximum}",
            suggestions=[
                "Wait for sessions to complete",
                "Increase maximum session limit",
                "Optimize session duration",
            ],
            **kwargs,
        )


class SessionCorruptedError(SessionError):
    """Session state is corrupted."""
    default_retryable = False
    default_severity = Severity.CRITICAL

    def __init__(self, session_id: str = "", reason: str = "", **kwargs):
        super().__init__(
            message=f"Session corrupted: {session_id} - {reason}",
            suggestions=[
                "Destroy and recreate session",
                "Check for memory corruption",
                "Review session state mutations",
            ],
            **kwargs,
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# CAPTCHA Errors
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class CaptchaError(BotException):
    """Base CAPTCHA error."""
    default_severity = Severity.WARNING
    default_category = ErrorCategory.CAPTCHA
    default_retryable = True
    default_retry_delay = 5.0


class CaptchaDetectedError(CaptchaError):
    """CAPTCHA challenge detected."""

    def __init__(self, captcha_type: str = "unknown", url: str = "", **kwargs):
        super().__init__(
            message=f"CAPTCHA detected: type={captcha_type} at {url}",
            suggestions=[
                "Send to CAPTCHA solving service",
                "Rotate proxy and retry",
                "Reduce request rate",
                "Improve fingerprint quality",
            ],
            **kwargs,
        )
        self.captcha_type = captcha_type


class CaptchaSolveFailedError(CaptchaError):
    """CAPTCHA solving attempt failed."""

    def __init__(self, service: str = "", reason: str = "", **kwargs):
        super().__init__(
            message=f"CAPTCHA solve failed via {service}: {reason}",
            suggestions=[
                "Try a different solving service",
                "Check solver API balance",
                "Verify solver API key",
            ],
            **kwargs,
        )


class CaptchaServiceError(CaptchaError):
    """CAPTCHA solving service unavailable."""
    default_retry_delay = 30.0

    def __init__(self, service: str = "", **kwargs):
        super().__init__(
            message=f"CAPTCHA service unavailable: {service}",
            suggestions=[
                "Check service status page",
                "Switch to backup solver",
                "Check API key validity and balance",
            ],
            **kwargs,
        )


class CaptchaBudgetExceededError(CaptchaError):
    """CAPTCHA solving budget exceeded."""
    default_retryable = False
    default_severity = Severity.CRITICAL

    def __init__(self, spent: float = 0, budget: float = 0, **kwargs):
        super().__init__(
            message=f"CAPTCHA budget exceeded: ${spent:.2f} / ${budget:.2f}",
            suggestions=[
                "Increase CAPTCHA budget",
                "Reduce traffic to CAPTCHA-heavy targets",
                "Improve evasion to reduce CAPTCHA encounters",
            ],
            **kwargs,
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Detection / Anti-Bot Errors
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class DetectionError(BotException):
    """Base detection/anti-bot error."""
    default_severity = Severity.CRITICAL
    default_category = ErrorCategory.DETECTION
    default_retryable = True
    default_retry_delay = 10.0


class BotDetectedError(DetectionError):
    """Bot behavior was detected by target."""

    def __init__(
        self,
        detection_system: str = "unknown",
        signals: Optional[List[str]] = None,
        **kwargs,
    ):
        detected_signals = signals or []
        super().__init__(
            message=(
                f"Bot detected by {detection_system}: "
                f"signals={', '.join(detected_signals)}"
            ),
            suggestions=[
                "Rotate all identifiers (proxy, fingerprint, UA)",
                "Increase behavioral randomization",
                "Reduce request frequency",
                "Update fingerprint profiles",
                "Add more human-like delays",
            ],
            **kwargs,
        )
        self.detection_system = detection_system
        self.signals = detected_signals


class CloudflareBlockError(DetectionError):
    """Blocked by Cloudflare protection."""

    def __init__(self, challenge_type: str = "", **kwargs):
        super().__init__(
            message=f"Cloudflare block: challenge={challenge_type}",
            suggestions=[
                "Use residential proxies",
                "Improve TLS fingerprint",
                "Handle Cloudflare challenge tokens",
                "Reduce request rate significantly",
            ],
            **kwargs,
        )


class RateLimitError(DetectionError):
    """Rate limit exceeded on target."""
    default_severity = Severity.WARNING
    default_retryable = True
    default_retry_delay = 60.0

    def __init__(self, limit: str = "", retry_after: float = 0, **kwargs):
        delay = retry_after if retry_after > 0 else 60.0
        super().__init__(
            message=f"Rate limited: {limit}, retry after {delay}s",
            retry_delay=delay,
            suggestions=[
                "Reduce request frequency",
                "Distribute across more proxies",
                "Implement proper request spacing",
            ],
            **kwargs,
        )
        self.retry_after = delay


class IPBlockedError(DetectionError):
    """IP address has been blocked."""
    default_retryable = False

    def __init__(self, ip: str = "", target: str = "", **kwargs):
        super().__init__(
            message=f"IP blocked: {ip} by {target}",
            suggestions=[
                "Switch to a different proxy",
                "Use residential/mobile proxy",
                "Wait 24-48 hours before retrying with this IP",
            ],
            **kwargs,
        )


class HoneypotDetectedError(DetectionError):
    """Honeypot trap detected."""
    default_severity = Severity.CRITICAL
    default_retryable = False

    def __init__(self, element_info: str = "", **kwargs):
        super().__init__(
            message=f"Honeypot detected: {element_info}",
            suggestions=[
                "Update honeypot detection rules",
                "Avoid interacting with hidden elements",
                "Improve CSS visibility checks",
            ],
            **kwargs,
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Encryption Errors
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class EncryptionError(BotException):
    """Base encryption error."""
    default_severity = Severity.CRITICAL
    default_category = ErrorCategory.ENCRYPTION
    default_retryable = False


class KeyGenerationError(EncryptionError):
    """Failed to generate encryption key."""

    def __init__(self, algorithm: str = "", **kwargs):
        super().__init__(
            message=f"Key generation failed for {algorithm}",
            suggestions=[
                "Check system entropy source",
                "Verify cryptographic library installation",
            ],
            **kwargs,
        )


class DecryptionError(EncryptionError):
    """Failed to decrypt data."""

    def __init__(self, reason: str = "", **kwargs):
        super().__init__(
            message=f"Decryption failed: {reason}",
            suggestions=[
                "Verify encryption key is correct",
                "Check data integrity",
                "Re-encrypt data with correct key",
            ],
            **kwargs,
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Analytics Errors
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class AnalyticsError(BotException):
    """Base analytics simulation error."""
    default_severity = Severity.WARNING
    default_category = ErrorCategory.ANALYTICS
    default_retryable = True


class GA4SimulationError(AnalyticsError):
    """Google Analytics 4 simulation failed."""

    def __init__(self, event_name: str = "", reason: str = "", **kwargs):
        super().__init__(
            message=f"GA4 simulation failed for event '{event_name}': {reason}",
            suggestions=[
                "Update GA4 measurement protocol parameters",
                "Check measurement ID format",
                "Verify event parameter schema",
            ],
            **kwargs,
        )


class PixelSimulationError(AnalyticsError):
    """Facebook/Meta Pixel simulation failed."""

    def __init__(self, pixel_id: str = "", reason: str = "", **kwargs):
        super().__init__(
            message=f"Pixel simulation failed (ID: {pixel_id}): {reason}",
            suggestions=[
                "Update pixel event parameters",
                "Check pixel ID validity",
                "Verify Meta pixel API version",
            ],
            **kwargs,
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Behavior Errors
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class BehaviorError(BotException):
    """Base behavior simulation error."""
    default_severity = Severity.WARNING
    default_category = ErrorCategory.BEHAVIOR
    default_retryable = True


class MouseSimulationError(BehaviorError):
    """Mouse movement simulation failed."""

    def __init__(self, reason: str = "", **kwargs):
        super().__init__(
            message=f"Mouse simulation failed: {reason}",
            suggestions=[
                "Check viewport dimensions",
                "Verify target element exists",
                "Reset mouse position to safe zone",
            ],
            **kwargs,
        )


class KeyboardSimulationError(BehaviorError):
    """Keyboard input simulation failed."""

    def __init__(self, reason: str = "", **kwargs):
        super().__init__(
            message=f"Keyboard simulation failed: {reason}",
            suggestions=[
                "Verify input field is focused",
                "Check element interactability",
            ],
            **kwargs,
        )


class ScrollSimulationError(BehaviorError):
    """Scroll simulation failed."""

    def __init__(self, reason: str = "", **kwargs):
        super().__init__(
            message=f"Scroll simulation failed: {reason}",
            suggestions=[
                "Check page has scrollable content",
                "Verify page is fully loaded",
            ],
            **kwargs,
        )


class BehavioralModelError(BehaviorError):
    """ML behavioral model error."""
    default_severity = Severity.ERROR

    def __init__(self, model_name: str = "", reason: str = "", **kwargs):
        super().__init__(
            message=f"Behavioral model '{model_name}' error: {reason}",
            suggestions=[
                "Retrain behavioral model",
                "Fall back to rule-based behavior",
                "Check model input data validity",
            ],
            **kwargs,
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Traffic Errors
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TrafficError(BotException):
    """Base traffic orchestration error."""
    default_severity = Severity.ERROR
    default_category = ErrorCategory.TRAFFIC
    default_retryable = True


class TrafficOrchestrationError(TrafficError):
    """Traffic orchestration failed."""
    default_severity = Severity.CRITICAL

    def __init__(self, reason: str = "", **kwargs):
        super().__init__(
            message=f"Traffic orchestration failed: {reason}",
            suggestions=[
                "Check all subsystems are healthy",
                "Reduce traffic volume",
                "Verify target URLs are accessible",
            ],
            **kwargs,
        )


class SearchSimulationError(TrafficError):
    """Search engine simulation failed."""

    def __init__(self, engine: str = "", query: str = "", reason: str = "", **kwargs):
        super().__init__(
            message=f"Search simulation failed on {engine} for '{query}': {reason}",
            suggestions=[
                "Update search engine selectors",
                "Check for CAPTCHA on search page",
                "Use different search keywords",
            ],
            **kwargs,
        )


class NavigationError(TrafficError):
    """Page navigation error."""

    def __init__(self, from_url: str = "", to_url: str = "", reason: str = "", **kwargs):
        super().__init__(
            message=f"Navigation failed: {from_url} -> {to_url}: {reason}",
            suggestions=[
                "Check target link exists on page",
                "Verify page has loaded completely",
                "Try alternative navigation path",
            ],
            **kwargs,
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Network Errors
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class NetworkError(BotException):
    """Base network error."""
    default_severity = Severity.ERROR
    default_category = ErrorCategory.NETWORK
    default_retryable = True
    default_retry_delay = 5.0


class ConnectionTimeoutError(NetworkError):
    """Connection timed out."""

    def __init__(self, url: str = "", timeout: float = 0, **kwargs):
        super().__init__(
            message=f"Connection timeout after {timeout}s: {url}",
            suggestions=[
                "Increase timeout value",
                "Check network connectivity",
                "Try a different proxy",
            ],
            **kwargs,
        )


class DNSResolutionError(NetworkError):
    """DNS resolution failed."""

    def __init__(self, domain: str = "", **kwargs):
        super().__init__(
            message=f"DNS resolution failed for: {domain}",
            suggestions=[
                "Check domain exists",
                "Try alternative DNS server",
                "Verify proxy DNS settings",
            ],
            **kwargs,
        )


class SSLError(NetworkError):
    """SSL/TLS error."""

    def __init__(self, url: str = "", reason: str = "", **kwargs):
        super().__init__(
            message=f"SSL error for {url}: {reason}",
            suggestions=[
                "Check SSL certificate validity",
                "Update CA certificates",
                "Disable SSL verification (testing only)",
            ],
            **kwargs,
        )


class VPNChainError(NetworkError):
    """VPN chain error."""
    default_severity = Severity.CRITICAL

    def __init__(self, hop: int = 0, reason: str = "", **kwargs):
        super().__init__(
            message=f"VPN chain error at hop {hop}: {reason}",
            suggestions=[
                "Check VPN server availability",
                "Reduce VPN chain length",
                "Verify VPN credentials",
            ],
            **kwargs,
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# System Errors
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class SystemError(BotException):
    """Base system error."""
    default_severity = Severity.CRITICAL
    default_category = ErrorCategory.SYSTEM
    default_retryable = False


class ResourceExhaustedError(SystemError):
    """System resources exhausted."""

    def __init__(self, resource: str = "", usage: str = "", **kwargs):
        super().__init__(
            message=f"Resource exhausted: {resource} ({usage})",
            suggestions=[
                "Reduce concurrent sessions",
                "Increase system resources",
                "Optimize memory usage",
                "Close unused browser instances",
            ],
            **kwargs,
        )


class SelfHealingError(SystemError):
    """Self-healing system error."""
    default_severity = Severity.FATAL

    def __init__(self, component: str = "", reason: str = "", **kwargs):
        super().__init__(
            message=f"Self-healing failed for '{component}': {reason}",
            suggestions=[
                "Manual intervention required",
                "Restart the application",
                "Check system logs for root cause",
            ],
            **kwargs,
        )


class PluginError(SystemError):
    """Plugin/module loading error."""
    default_severity = Severity.ERROR

    def __init__(self, plugin_name: str = "", reason: str = "", **kwargs):
        super().__init__(
            message=f"Plugin error '{plugin_name}': {reason}",
            suggestions=[
                "Check plugin compatibility",
                "Reinstall plugin dependencies",
                "Disable and re-enable plugin",
            ],
            **kwargs,
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# GUI Errors
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class GUIError(BotException):
    """Base GUI error."""
    default_severity = Severity.WARNING
    default_category = ErrorCategory.GUI
    default_retryable = False


class GUIRenderError(GUIError):
    """GUI rendering error."""

    def __init__(self, component: str = "", reason: str = "", **kwargs):
        super().__init__(
            message=f"GUI render error in '{component}': {reason}",
            suggestions=[
                "Restart GUI component",
                "Check display settings",
                "Update GUI framework",
            ],
            **kwargs,
        )


class ChartUpdateError(GUIError):
    """Chart update error."""

    def __init__(self, chart_type: str = "", reason: str = "", **kwargs):
        super().__init__(
            message=f"Chart update failed for '{chart_type}': {reason}",
            suggestions=[
                "Reset chart data buffer",
                "Check data format",
            ],
            **kwargs,
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Exception Registry (for monitoring and self-healing)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class ExceptionRegistry:
    """
    Central registry that tracks all exceptions for analytics,
    monitoring, and self-healing decisions.
    """

    def __init__(self, max_history: int = 10000):
        self._history: List[Dict[str, Any]] = []
        self._fingerprint_counts: Dict[str, int] = {}
        self._category_counts: Dict[str, int] = {}
        self._max_history = max_history

    def record(self, exception: BotException) -> None:
        """Record an exception occurrence."""
        entry = exception.to_dict()
        self._history.append(entry)

        # Track fingerprint frequency
        fp = exception.fingerprint
        self._fingerprint_counts[fp] = self._fingerprint_counts.get(fp, 0) + 1

        # Track category frequency
        cat = exception.category
        self._category_counts[cat] = self._category_counts.get(cat, 0) + 1

        # Trim history if needed
        if len(self._history) > self._max_history:
            removed = self._history[:len(self._history) - self._max_history]
            self._history = self._history[-self._max_history:]
            # Adjust counts for removed entries
            for item in removed:
                fp_key = item.get("fingerprint", "")
                if fp_key in self._fingerprint_counts:
                    self._fingerprint_counts[fp_key] = max(
                        0, self._fingerprint_counts[fp_key] - 1
                    )

    def get_frequency(self, fingerprint: str) -> int:
        """Get occurrence count for an error fingerprint."""
        return self._fingerprint_counts.get(fingerprint, 0)

    def get_category_stats(self) -> Dict[str, int]:
        """Get error counts by category."""
        return self._category_counts.copy()

    def get_recent(self, count: int = 50) -> List[Dict[str, Any]]:
        """Get most recent exceptions."""
        return self._history[-count:]

    def get_most_frequent(self, top_n: int = 10) -> List[tuple]:
        """Get most frequently occurring errors."""
        sorted_fps = sorted(
            self._fingerprint_counts.items(),
            key=lambda x: x[1],
            reverse=True,
        )
        return sorted_fps[:top_n]

    def is_recurring(self, fingerprint: str, threshold: int = 5) -> bool:
        """Check if an error is recurring above threshold."""
        return self.get_frequency(fingerprint) >= threshold

    def clear(self) -> None:
        """Clear all history."""
        self._history.clear()
        self._fingerprint_counts.clear()
        self._category_counts.clear()

    @property
    def total_errors(self) -> int:
        return len(self._history)

    def summary(self) -> Dict[str, Any]:
        """Get a summary of all recorded exceptions."""
        return {
            "total_errors": self.total_errors,
            "unique_errors": len(self._fingerprint_counts),
            "category_breakdown": self._category_counts.copy(),
            "most_frequent": self.get_most_frequent(5),
        }


# Global exception registry instance
global_exception_registry = ExceptionRegistry()