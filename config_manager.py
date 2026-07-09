"""
Jubra Traffic Pro - Encrypted Configuration Manager
AES-256-GCM encrypted config with hot-reload, schema validation,
environment variable override, and change notification via EventBus.
"""

import os
import io
import json
import time
import copy
import shutil
import asyncio
import hashlib
import logging
import secrets
import threading
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple, Union, Callable

import yaml

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
    from cryptography.hazmat.backends import default_backend
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    HAS_CRYPTOGRAPHY = True
except ImportError:
    HAS_CRYPTOGRAPHY = False
    logging.warning("[ConfigManager] cryptography not installed — encryption disabled")

try:
    import jsonschema
    HAS_JSONSCHEMA = True
except ImportError:
    HAS_JSONSCHEMA = False

from core.exceptions import (
    ConfigError,
    ConfigFileNotFoundError,
    ConfigValidationError,
    ConfigEncryptionError,
    ConfigHotReloadError,
    ErrorContext,
)
from core.event_bus import EventBus, EventCategory, EventPriority, get_event_bus

logger = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Configuration Schema
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

DEFAULT_CONFIG_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "required": ["general", "proxy", "browser", "traffic", "behavior"],
    "properties": {
        "general": {
            "type": "object",
            "properties": {
                "version":              {"type": "string"},
                "debug":                {"type": "boolean"},
                "log_level":            {"type": "string", "enum": ["DEBUG", "INFO", "WARNING", "ERROR"]},
                "max_workers":          {"type": "integer", "minimum": 1, "maximum": 500},
                "session_timeout":      {"type": "number", "minimum": 30},
                "data_dir":             {"type": "string"},
                "enable_encryption":    {"type": "boolean"},
                "locale":               {"type": "string"},
                "timezone":             {"type": "string"},
            },
        },
        "proxy": {
            "type": "object",
            "properties": {
                "enabled":              {"type": "boolean"},
                "pool_file":            {"type": "string"},
                "rotation_strategy":    {"type": "string", "enum": ["round_robin", "weighted", "random", "sticky", "least_used"]},
                "health_check_interval": {"type": "number", "minimum": 10},
                "health_check_timeout": {"type": "number", "minimum": 1},
                "max_failures":         {"type": "integer", "minimum": 1},
                "ban_duration":         {"type": "number", "minimum": 60},
                "protocols":            {"type": "array", "items": {"type": "string"}},
                "tor_enabled":          {"type": "boolean"},
                "tor_control_port":     {"type": "integer"},
                "tor_control_password": {"type": "string"},
                "tor_circuit_ttl":      {"type": "number"},
                "residential_only":     {"type": "boolean"},
                "min_pool_size":        {"type": "integer", "minimum": 1},
                "authentication":       {"type": "object"},
            },
        },
        "browser": {
            "type": "object",
            "properties": {
                "pool_size":            {"type": "integer", "minimum": 1, "maximum": 100},
                "headless":             {"type": "boolean"},
                "chrome_path":          {"type": "string"},
                "chromedriver_path":    {"type": "string"},
                "page_load_timeout":    {"type": "number", "minimum": 5},
                "script_timeout":       {"type": "number", "minimum": 1},
                "implicit_wait":        {"type": "number", "minimum": 0},
                "window_width":         {"type": "integer", "minimum": 320},
                "window_height":        {"type": "integer", "minimum": 240},
                "disable_images":       {"type": "boolean"},
                "disable_javascript":   {"type": "boolean"},
                "extra_arguments":      {"type": "array", "items": {"type": "string"}},
                "mobile_emulation":     {"type": "boolean"},
                "mobile_device":        {"type": "string"},
                "warmup_count":         {"type": "integer", "minimum": 0},
                "recycle_after":        {"type": "integer", "minimum": 1},
                "crash_recovery":       {"type": "boolean"},
            },
        },
        "fingerprint": {
            "type": "object",
            "properties": {
                "enabled":              {"type": "boolean"},
                "canvas_spoofing":      {"type": "boolean"},
                "audio_spoofing":       {"type": "boolean"},
                "webgl_spoofing":       {"type": "boolean"},
                "tls_spoofing":         {"type": "boolean"},
                "font_spoofing":        {"type": "boolean"},
                "timezone_spoofing":    {"type": "boolean"},
                "language_spoofing":    {"type": "boolean"},
                "mutation_rate":        {"type": "number", "minimum": 0.0, "maximum": 1.0},
                "profiles_file":        {"type": "string"},
                "consistency_checks":   {"type": "boolean"},
            },
        },
        "traffic": {
            "type": "object",
            "properties": {
                "target_urls":          {"type": "array", "items": {"type": "string"}},
                "sessions_per_hour":    {"type": "integer", "minimum": 1},
                "daily_limit":          {"type": "integer", "minimum": 0},
                "organic_ratio":        {"type": "number", "minimum": 0.0, "maximum": 1.0},
                "social_ratio":         {"type": "number", "minimum": 0.0, "maximum": 1.0},
                "direct_ratio":         {"type": "number", "minimum": 0.0, "maximum": 1.0},
                "referral_ratio":       {"type": "number", "minimum": 0.0, "maximum": 1.0},
                "geo_distribution":     {"type": "object"},
                "device_distribution":  {"type": "object"},
                "search_engines":       {"type": "array"},
                "keywords_file":        {"type": "string"},
                "min_session_duration": {"type": "number", "minimum": 5},
                "max_session_duration": {"type": "number", "minimum": 10},
                "pages_per_session":    {"type": "object"},
                "bounce_rate":          {"type": "number", "minimum": 0.0, "maximum": 1.0},
                "schedule":             {"type": "object"},
            },
        },
        "behavior": {
            "type": "object",
            "properties": {
                "mouse_enabled":        {"type": "boolean"},
                "scroll_enabled":       {"type": "boolean"},
                "keyboard_enabled":     {"type": "boolean"},
                "typing_wpm_min":       {"type": "integer", "minimum": 10},
                "typing_wpm_max":       {"type": "integer", "minimum": 10},
                "scroll_speed":         {"type": "string", "enum": ["slow", "normal", "fast", "random"]},
                "click_delay_min":      {"type": "number", "minimum": 0.0},
                "click_delay_max":      {"type": "number", "minimum": 0.0},
                "attention_model":      {"type": "boolean"},
                "ml_adaptation":        {"type": "boolean"},
                "idle_probability":     {"type": "number", "minimum": 0.0, "maximum": 1.0},
                "read_time_factor":     {"type": "number", "minimum": 0.1},
            },
        },
        "analytics": {
            "type": "object",
            "properties": {
                "ga4_enabled":          {"type": "boolean"},
                "ga4_measurement_id":   {"type": "string"},
                "ga4_api_secret":       {"type": "string"},
                "pixel_enabled":        {"type": "boolean"},
                "pixel_id":             {"type": "string"},
                "heatmap_enabled":      {"type": "boolean"},
                "heatmap_provider":     {"type": "string"},
            },
        },
        "security": {
            "type": "object",
            "properties": {
                "captcha_service":      {"type": "string", "enum": ["2captcha", "anticaptcha", "capmonster", "manual", "none"]},
                "captcha_api_key":      {"type": "string"},
                "captcha_budget":       {"type": "number", "minimum": 0},
                "encryption_enabled":   {"type": "boolean"},
                "obfuscate_config":     {"type": "boolean"},
            },
        },
        "monitoring": {
            "type": "object",
            "properties": {
                "metrics_enabled":      {"type": "boolean"},
                "metrics_interval":     {"type": "number", "minimum": 1},
                "self_healing":         {"type": "boolean"},
                "alert_email":          {"type": "string"},
                "webhook_url":          {"type": "string"},
                "dashboard_port":       {"type": "integer"},
                "performance_sampling": {"type": "boolean"},
            },
        },
        "gui": {
            "type": "object",
            "properties": {
                "enabled":              {"type": "boolean"},
                "theme":                {"type": "string", "enum": ["dark", "light", "system"]},
                "refresh_interval":     {"type": "number", "minimum": 0.1},
                "chart_history":        {"type": "integer", "minimum": 10},
                "log_lines":            {"type": "integer", "minimum": 50},
                "window_width":         {"type": "integer", "minimum": 800},
                "window_height":        {"type": "integer", "minimum": 600},
                "always_on_top":        {"type": "boolean"},
            },
        },
    },
}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Encryption Manager (internal)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class _ConfigEncryption:
    """AES-256-GCM encryption for config data."""

    SALT_SIZE    = 32
    NONCE_SIZE   = 12
    KEY_SIZE     = 32
    SCRYPT_N     = 2 ** 15  # CPU cost (balance of security vs speed)
    SCRYPT_R     = 8
    SCRYPT_P     = 1
    MAGIC_BYTES  = b"STBV6\x00"

    def __init__(self, password: str):
        if not HAS_CRYPTOGRAPHY:
            raise ConfigEncryptionError(
                "encrypt",
                context=ErrorContext(module="ConfigEncryption"),
            )
        self._password = password.encode("utf-8")

    def _derive_key(self, salt: bytes) -> bytes:
        """Derive AES key from password using scrypt KDF."""
        kdf = Scrypt(
            salt=salt,
            length=self.KEY_SIZE,
            n=self.SCRYPT_N,
            r=self.SCRYPT_R,
            p=self.SCRYPT_P,
            backend=default_backend(),
        )
        return kdf.derive(self._password)

    def encrypt(self, plaintext: bytes) -> bytes:
        """Encrypt data with AES-256-GCM. Returns: MAGIC + SALT + NONCE + TAG + CIPHERTEXT."""
        salt  = secrets.token_bytes(self.SALT_SIZE)
        nonce = secrets.token_bytes(self.NONCE_SIZE)
        key   = self._derive_key(salt)
        aesgcm = AESGCM(key)
        ciphertext = aesgcm.encrypt(nonce, plaintext, None)
        return self.MAGIC_BYTES + salt + nonce + ciphertext

    def decrypt(self, ciphertext: bytes) -> bytes:
        """Decrypt AES-256-GCM encrypted data."""
        magic_len = len(self.MAGIC_BYTES)
        if ciphertext[:magic_len] != self.MAGIC_BYTES:
            raise ConfigEncryptionError("decrypt")

        offset = magic_len
        salt  = ciphertext[offset:offset + self.SALT_SIZE]
        offset += self.SALT_SIZE
        nonce = ciphertext[offset:offset + self.NONCE_SIZE]
        offset += self.NONCE_SIZE
        payload = ciphertext[offset:]

        key = self._derive_key(salt)
        aesgcm = AESGCM(key)
        try:
            return aesgcm.decrypt(nonce, payload, None)
        except Exception as e:
            raise ConfigEncryptionError("decrypt") from e

    @staticmethod
    def is_encrypted(data: bytes) -> bool:
        return data[:len(_ConfigEncryption.MAGIC_BYTES)] == _ConfigEncryption.MAGIC_BYTES


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Config Change Tracker
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class ConfigChangeTracker:
    """Tracks what changed between two config versions."""

    def __init__(self):
        self._changes: List[Dict[str, Any]] = []

    def diff(
        self,
        old: Dict[str, Any],
        new: Dict[str, Any],
        prefix: str = "",
    ) -> List[Dict[str, Any]]:
        """Recursively compute config diff."""
        self._changes.clear()
        self._recursive_diff(old, new, prefix)
        return self._changes.copy()

    def _recursive_diff(
        self,
        old: Any,
        new: Any,
        path: str,
    ) -> None:
        if isinstance(old, dict) and isinstance(new, dict):
            all_keys = set(old.keys()) | set(new.keys())
            for key in all_keys:
                child_path = f"{path}.{key}" if path else key
                if key not in old:
                    self._changes.append({
                        "path":   child_path,
                        "type":   "added",
                        "old":    None,
                        "new":    new[key],
                    })
                elif key not in new:
                    self._changes.append({
                        "path":   child_path,
                        "type":   "removed",
                        "old":    old[key],
                        "new":    None,
                    })
                else:
                    self._recursive_diff(old[key], new[key], child_path)
        else:
            if old != new:
                self._changes.append({
                    "path":   path,
                    "type":   "modified",
                    "old":    old,
                    "new":    new,
                })


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Config Manager
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class ConfigManager:
    """
    Jubra Traffic Pro - Configuration Manager

    Features:
    ─────────────────────────────────────────────────────
    • AES-256-GCM encrypted config files
    • YAML/JSON config file support
    • JSON Schema validation with helpful error messages
    • Hot-reload with file-watcher (inotify on Linux, polling fallback)
    • Environment variable overrides (STB_PROXY_ENABLED=true)
    • Deep get/set with dot-notation (e.g., 'proxy.rotation_strategy')
    • Config versioning and change tracking
    • Atomic writes with backup (no partial writes)
    • Change listeners with EventBus integration
    • Default value injection
    • Secret masking in logs
    """

    DEFAULT_CONFIG_PATH = Path("config/default_config.yaml")
    BACKUP_SUFFIX       = ".backup"
    ENV_PREFIX          = "STB_"
    SECRETS_KEYS: Set[str] = {
        "api_key", "password", "secret", "token", "captcha_api_key",
        "ga4_api_secret", "tor_control_password", "webhook_url",
    }

    def __init__(
        self,
        config_path:        Union[str, Path] = "config/default_config.yaml",
        encryption_password: Optional[str]   = None,
        enable_hot_reload:  bool             = True,
        hot_reload_interval: float           = 3.0,
        validate_schema:    bool             = True,
        event_bus:          Optional[EventBus] = None,
        auto_create:        bool             = True,
    ):
        self._path          = Path(config_path)
        self._encryption    = (
            _ConfigEncryption(encryption_password)
            if encryption_password and HAS_CRYPTOGRAPHY
            else None
        )
        self._enable_hot_reload    = enable_hot_reload
        self._hot_reload_interval  = hot_reload_interval
        self._validate_schema      = validate_schema
        self._event_bus            = event_bus or get_event_bus()
        self._auto_create          = auto_create

        self._config:       Dict[str, Any]  = {}
        self._defaults:     Dict[str, Any]  = {}
        self._last_hash:    str             = ""
        self._last_loaded:  float           = 0.0
        self._load_count:   int             = 0
        self._lock          = threading.RLock()
        self._async_lock    = asyncio.Lock()
        self._change_tracker = ConfigChangeTracker()
        self._watchers:     List[Callable]  = []
        self._reload_task:  Optional[asyncio.Task] = None

        self._set_defaults()

        logger.info(
            f"[ConfigManager] Initialized: path={self._path}, "
            f"encrypted={self._encryption is not None}, "
            f"hot_reload={enable_hot_reload}"
        )

    # ── Public API ─────────────────────────────────────────

    def load(self) -> Dict[str, Any]:
        """Load and parse configuration. Raises on error."""
        with self._lock:
            return self._load_internal()

    async def load_async(self) -> Dict[str, Any]:
        """Async version of load."""
        async with self._async_lock:
            loop = asyncio.get_event_loop()
            config = await loop.run_in_executor(None, self._load_internal)
            return config

    def get(self, key: str, default: Any = None) -> Any:
        """
        Get a config value using dot-notation.
        e.g., config.get('proxy.rotation_strategy')
        Environment variable override is applied automatically.
        """
        with self._lock:
            # Check ENV override first
            env_val = self._get_env_override(key)
            if env_val is not None:
                return env_val

            # Traverse config dict
            parts = key.split(".")
            current = self._config
            for part in parts:
                if not isinstance(current, dict) or part not in current:
                    # Try defaults
                    return self._get_default(key, default)
                current = current[part]
            return current

    def get_section(self, section: str) -> Dict[str, Any]:
        """Get an entire config section as a dict."""
        with self._lock:
            return copy.deepcopy(self._config.get(section, {}))

    def set(self, key: str, value: Any, persist: bool = False) -> None:
        """
        Set a config value at runtime using dot-notation.
        Optionally persists to disk.
        """
        with self._lock:
            parts = key.split(".")
            current = self._config
            for part in parts[:-1]:
                if part not in current:
                    current[part] = {}
                current = current[part]
            old_value = current.get(parts[-1])
            current[parts[-1]] = value

            if old_value != value:
                self._notify_change(key, old_value, value)

            if persist:
                self._save_internal()

    def set_bulk(self, updates: Dict[str, Any], persist: bool = False) -> None:
        """Set multiple values at once."""
        with self._lock:
            for key, value in updates.items():
                parts = key.split(".")
                current = self._config
                for part in parts[:-1]:
                    current = current.setdefault(part, {})
                current[parts[-1]] = value

            if persist:
                self._save_internal()

    def save(self) -> None:
        """Persist current config to disk."""
        with self._lock:
            self._save_internal()

    async def save_async(self) -> None:
        async with self._async_lock:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self._save_internal)

    def add_watcher(self, callback: Callable[[str, Any, Any], None]) -> None:
        """
        Register a change watcher.
        callback(key: str, old_value: Any, new_value: Any)
        """
        self._watchers.append(callback)

    def remove_watcher(self, callback: Callable) -> None:
        self._watchers = [w for w in self._watchers if w != callback]

    async def start_hot_reload(self) -> None:
        """Start background hot-reload watcher."""
        if not self._enable_hot_reload:
            return
        if self._reload_task and not self._reload_task.done():
            return

        self._reload_task = asyncio.create_task(
            self._hot_reload_loop(),
            name="ConfigManager-HotReload",
        )
        logger.info(
            f"[ConfigManager] Hot-reload started: "
            f"interval={self._hot_reload_interval}s"
        )

    async def stop_hot_reload(self) -> None:
        """Stop hot-reload watcher."""
        if self._reload_task:
            self._reload_task.cancel()
            try:
                await self._reload_task
            except asyncio.CancelledError:
                pass
            self._reload_task = None
        logger.info("[ConfigManager] Hot-reload stopped")

    def validate(self) -> Tuple[bool, List[str]]:
        """
        Validate current config against schema.
        Returns (is_valid, list_of_errors).
        """
        if not HAS_JSONSCHEMA:
            return True, []

        errors = []
        try:
            validator = jsonschema.Draft7Validator(DEFAULT_CONFIG_SCHEMA)
            for error in validator.iter_errors(self._config):
                path = ".".join(str(p) for p in error.absolute_path)
                errors.append(f"{path}: {error.message}")
        except Exception as exc:
            errors.append(f"Schema validation error: {exc}")

        return len(errors) == 0, errors

    def get_masked(self) -> Dict[str, Any]:
        """
        Get config with sensitive values masked for logging.
        """
        with self._lock:
            return self._mask_secrets(copy.deepcopy(self._config))

    def reload(self) -> bool:
        """
        Force an immediate config reload.
        Returns True if config changed.
        """
        with self._lock:
            try:
                new_config = self._parse_file()
                old_config = copy.deepcopy(self._config)
                raw = self._path.read_bytes()
                if self._encryption and _ConfigEncryption.is_encrypted(raw):
                    raw = self._encryption.decrypt(raw)
                current_hash = hashlib.sha256(raw).hexdigest()
                changes = self._change_tracker.diff(old_config, new_config)

                # Always refresh the file hash after a successful reload attempt.
                # Without this, GUI saves that already updated the in-memory config
                # caused the hot-reload loop to detect the same file change forever.
                self._last_hash = current_hash
                self._last_loaded = time.time()

                if changes:
                    self._config = new_config
                    self._load_count += 1
                    logger.info(
                        f"[ConfigManager] Reloaded: {len(changes)} changes"
                    )
                    self._emit_reload_event(changes)
                    return True
                return False

            except Exception as exc:
                logger.error(f"[ConfigManager] Reload failed: {exc}")
                raise ConfigHotReloadError(str(exc)) from exc

    def generate_default(self) -> None:
        """Generate a default config file at the configured path."""
        default = self._build_default_config()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._path, "w", encoding="utf-8") as f:
            yaml.dump(default, f, default_flow_style=False, indent=2, sort_keys=True)
        logger.info(f"[ConfigManager] Default config created: {self._path}")

    def export_json(self, mask_secrets: bool = True) -> str:
        """Export current config as JSON string."""
        config = self.get_masked() if mask_secrets else copy.deepcopy(self._config)
        return json.dumps(config, indent=2, default=str)

    def stats(self) -> Dict[str, Any]:
        return {
            "config_path":       str(self._path),
            "encrypted":         self._encryption is not None,
            "hot_reload":        self._enable_hot_reload,
            "load_count":        self._load_count,
            "last_loaded":       self._last_loaded,
            "last_hash":         self._last_hash[:12] + "..." if self._last_hash else "",
            "watcher_count":     len(self._watchers),
            "config_keys":       len(self._flatten_keys(self._config)),
        }

    # ── Internal Loading ───────────────────────────────────

    def _load_internal(self) -> Dict[str, Any]:
        """Core load logic (must hold lock)."""
        if not self._path.exists():
            if self._auto_create:
                logger.warning(
                    f"[ConfigManager] Config not found, creating default: {self._path}"
                )
                self.generate_default()
            else:
                raise ConfigFileNotFoundError(str(self._path))

        try:
            raw = self._path.read_bytes()

            # Decrypt if needed
            if self._encryption and _ConfigEncryption.is_encrypted(raw):
                raw = self._encryption.decrypt(raw)

            # Compute hash for change detection
            content_hash = hashlib.sha256(raw).hexdigest()

            # Parse YAML or JSON
            if self._path.suffix in (".yaml", ".yml"):
                parsed = yaml.safe_load(raw.decode("utf-8")) or {}
            elif self._path.suffix == ".json":
                parsed = json.loads(raw.decode("utf-8"))
            else:
                # Try YAML first, then JSON
                try:
                    parsed = yaml.safe_load(raw.decode("utf-8")) or {}
                except yaml.YAMLError:
                    parsed = json.loads(raw.decode("utf-8"))

            # Deep merge with defaults
            merged = self._deep_merge(copy.deepcopy(self._defaults), parsed)

            # Apply environment variable overrides
            self._apply_env_overrides(merged)

            # Validate schema
            if self._validate_schema and HAS_JSONSCHEMA:
                self._validate_against_schema(merged)

            old_config = copy.deepcopy(self._config)
            self._config = merged
            self._last_hash = content_hash
            self._last_loaded = time.time()
            self._load_count += 1

            if old_config:
                changes = self._change_tracker.diff(old_config, merged)
                if changes:
                    self._emit_reload_event(changes)

            logger.info(
                f"[ConfigManager] Loaded: {self._path} "
                f"(hash={content_hash[:8]})"
            )

            asyncio.create_task(
                self._event_bus.publish_simple(
                    EventCategory.CONFIG_LOADED,
                    {
                        "path":       str(self._path),
                        "hash":       content_hash[:16],
                        "load_count": self._load_count,
                    },
                )
            ) if asyncio.get_event_loop().is_running() else None

            return merged

        except (ConfigError, ConfigEncryptionError):
            raise
        except Exception as exc:
            raise ConfigError(
                f"Failed to load config: {exc}",
                context=ErrorContext(module="ConfigManager", operation="load"),
            ) from exc

    def _save_internal(self) -> None:
        """Core save logic (must hold lock). Atomic write with backup."""
        self._path.parent.mkdir(parents=True, exist_ok=True)

        # Backup existing file
        backup_path = self._path.with_suffix(self.BACKUP_SUFFIX)
        if self._path.exists():
            shutil.copy2(self._path, backup_path)

        # Serialize to YAML
        content = yaml.dump(
            self._config,
            default_flow_style=False,
            indent=2,
            sort_keys=True,
            allow_unicode=True,
        ).encode("utf-8")

        # Encrypt if needed
        if self._encryption:
            content = self._encryption.encrypt(content)

        # Atomic write using temp file
        tmp_path = self._path.with_suffix(".tmp")
        try:
            tmp_path.write_bytes(content)
            tmp_path.replace(self._path)
        except Exception:
            # Restore backup on failure
            if backup_path.exists():
                backup_path.replace(self._path)
            raise

        self._last_hash = hashlib.sha256(content).hexdigest()
        self._last_loaded = time.time()

        logger.info(f"[ConfigManager] Saved: {self._path}")

    def _parse_file(self) -> Dict[str, Any]:
        """Parse config file without updating internal state."""
        raw = self._path.read_bytes()
        if self._encryption and _ConfigEncryption.is_encrypted(raw):
            raw = self._encryption.decrypt(raw)
        parsed = yaml.safe_load(raw.decode("utf-8")) or {}
        merged = self._deep_merge(copy.deepcopy(self._defaults), parsed)
        self._apply_env_overrides(merged)
        return merged

    # ── Environment Override ───────────────────────────────

    def _get_env_override(self, key: str) -> Optional[Any]:
        """Convert dot-key to ENV var and check os.environ."""
        env_key = self.ENV_PREFIX + key.upper().replace(".", "_")
        raw = os.environ.get(env_key)
        if raw is None:
            return None
        return self._coerce_env_value(raw)

    def _apply_env_overrides(self, config: Dict[str, Any]) -> None:
        """Apply all matching STB_ environment variables to config."""
        prefix_len = len(self.ENV_PREFIX)
        for env_key, env_val in os.environ.items():
            if not env_key.startswith(self.ENV_PREFIX):
                continue
            config_key = env_key[prefix_len:].lower().replace("_", ".")
            parts = config_key.split(".")
            current = config
            for part in parts[:-1]:
                if part not in current:
                    current[part] = {}
                current = current[part]
            if isinstance(current, dict):
                current[parts[-1]] = self._coerce_env_value(env_val)

    @staticmethod
    def _coerce_env_value(value: str) -> Any:
        """Convert string env var to appropriate Python type."""
        if value.lower() in ("true", "yes", "1", "on"):
            return True
        if value.lower() in ("false", "no", "0", "off"):
            return False
        try:
            return int(value)
        except ValueError:
            pass
        try:
            return float(value)
        except ValueError:
            pass
        # Try JSON (for lists, dicts)
        try:
            return json.loads(value)
        except (json.JSONDecodeError, ValueError):
            pass
        return value

    # ── Schema Validation ──────────────────────────────────

    def _validate_against_schema(self, config: Dict[str, Any]) -> None:
        """Raise ConfigValidationError if schema validation fails."""
        if not HAS_JSONSCHEMA:
            return
        validator = jsonschema.Draft7Validator(DEFAULT_CONFIG_SCHEMA)
        errors = list(validator.iter_errors(config))
        if errors:
            # Report the most critical error
            first = errors[0]
            path = ".".join(str(p) for p in first.absolute_path)
            raise ConfigValidationError(
                field=path or "root",
                value=first.instance,
                expected=first.schema.get("type", first.validator),
                context=ErrorContext(module="ConfigManager", operation="validate"),
            )

    # ── Hot Reload Loop ────────────────────────────────────

    async def _hot_reload_loop(self) -> None:
        """Background file watcher for hot-reload."""
        while True:
            try:
                await asyncio.sleep(self._hot_reload_interval)

                if not self._path.exists():
                    continue

                # Check file hash
                raw = self._path.read_bytes()
                current_hash = hashlib.sha256(raw).hexdigest()

                if current_hash != self._last_hash:
                    logger.info(
                        f"[ConfigManager] Change detected: {self._path}"
                    )
                    try:
                        changed = self.reload()
                        if changed:
                            await self._event_bus.publish_simple(
                                EventCategory.CONFIG_RELOADED,
                                {
                                    "path":  str(self._path),
                                    "hash":  current_hash[:16],
                                },
                                priority=EventPriority.HIGH,
                            )
                    except ConfigHotReloadError as exc:
                        logger.error(f"[ConfigManager] Hot-reload error: {exc}")
                        await self._event_bus.publish_simple(
                            EventCategory.CONFIG_ERROR,
                            {"error": str(exc)},
                            priority=EventPriority.HIGH,
                        )

            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error(f"[ConfigManager] Hot-reload loop error: {exc}")
                await asyncio.sleep(5.0)

    # ── Change Notification ────────────────────────────────

    def _notify_change(self, key: str, old_value: Any, new_value: Any) -> None:
        """Notify all watchers of a config change."""
        for watcher in self._watchers:
            try:
                watcher(key, old_value, new_value)
            except Exception as exc:
                logger.error(f"[ConfigManager] Watcher error: {exc}")

    def _emit_reload_event(self, changes: List[Dict[str, Any]]) -> None:
        """Emit reload event with diff information."""
        safe_changes = [
            c for c in changes
            if not any(s in c["path"].lower() for s in self.SECRETS_KEYS)
        ]
        logger.info(
            f"[ConfigManager] {len(changes)} config changes "
            f"(shown: {len(safe_changes)})"
        )
        for change in safe_changes[:10]:  # Log first 10
            logger.debug(
                f"  {change['type'].upper()}: {change['path']} "
                f"{change['old']!r} → {change['new']!r}"
            )

    # ── Utilities ──────────────────────────────────────────

    @staticmethod
    def _deep_merge(base: Dict, override: Dict) -> Dict:
        """Deep merge override into base. Override wins on conflict."""
        result = copy.deepcopy(base)
        for key, val in override.items():
            if (
                key in result
                and isinstance(result[key], dict)
                and isinstance(val, dict)
            ):
                result[key] = ConfigManager._deep_merge(result[key], val)
            else:
                result[key] = copy.deepcopy(val)
        return result

    def _get_default(self, key: str, fallback: Any = None) -> Any:
        """Look up a key in defaults."""
        parts = key.split(".")
        current = self._defaults
        for part in parts:
            if not isinstance(current, dict) or part not in current:
                return fallback
            current = current[part]
        return current

    def _mask_secrets(self, obj: Any, depth: int = 0) -> Any:
        """Recursively mask secret values."""
        if depth > 20:
            return obj
        if isinstance(obj, dict):
            return {
                k: "***MASKED***" if any(s in k.lower() for s in self.SECRETS_KEYS)
                else self._mask_secrets(v, depth + 1)
                for k, v in obj.items()
            }
        if isinstance(obj, list):
            return [self._mask_secrets(item, depth + 1) for item in obj]
        return obj

    @staticmethod
    def _flatten_keys(obj: Dict, prefix: str = "") -> List[str]:
        """Get all dot-notation keys from a nested dict."""
        keys = []
        for k, v in obj.items():
            full_key = f"{prefix}.{k}" if prefix else k
            if isinstance(v, dict):
                keys.extend(ConfigManager._flatten_keys(v, full_key))
            else:
                keys.append(full_key)
        return keys

    def _set_defaults(self) -> None:
        """Set hardcoded defaults for all config sections."""
        self._defaults = self._build_default_config()

    def _build_default_config(self) -> Dict[str, Any]:
        """Build complete default configuration."""
        return {
            "general": {
                "version":              "1.0.0",
                "debug":                False,
                "log_level":            "INFO",
                "max_workers":          10,
                "session_timeout":      300,
                "data_dir":             "data",
                "enable_encryption":    False,
                "locale":               "en-US",
                "timezone":             "America/New_York",
            },
            "proxy": {
                "enabled":              True,
                "pool_file":            "data/proxies.txt",
                "rotation_strategy":    "weighted",
                "health_check_interval": 60.0,
                "health_check_timeout": 10.0,
                "max_failures":         3,
                "ban_duration":         3600.0,
                "protocols":            ["http", "https", "socks5"],
                "tor_enabled":          False,
                "tor_control_port":     9051,
                "tor_control_password": "",
                "tor_circuit_ttl":      600.0,
                "residential_only":     False,
                "min_pool_size":        5,
                "authentication":       {},
            },
            "browser": {
                "pool_size":            5,
                "headless":             True,
                "chrome_path":          "",
                "chromedriver_path":    "",
                "page_load_timeout":    30.0,
                "script_timeout":       10.0,
                "implicit_wait":        0.0,
                "window_width":         1920,
                "window_height":        1080,
                "disable_images":       False,
                "disable_javascript":   False,
                "extra_arguments":      [],
                "mobile_emulation":     False,
                "mobile_device":        "Pixel 5",
                "warmup_count":         2,
                "recycle_after":        50,
                "crash_recovery":       True,
            },
            "fingerprint": {
                "enabled":              True,
                "canvas_spoofing":      True,
                "audio_spoofing":       True,
                "webgl_spoofing":       True,
                "tls_spoofing":         True,
                "font_spoofing":        True,
                "timezone_spoofing":    True,
                "language_spoofing":    True,
                "mutation_rate":        0.15,
                "profiles_file":        "data/fingerprints.json",
                "consistency_checks":   True,
            },
            "traffic": {
                "target_urls":          [],
                "sessions_per_hour":    30,
                "daily_limit":          0,
                "organic_ratio":        0.60,
                "social_ratio":         0.15,
                "direct_ratio":         0.15,
                "referral_ratio":       0.10,
                "geo_distribution":     {"US": 0.5, "GB": 0.2, "CA": 0.15, "AU": 0.15},
                "device_distribution":  {"desktop": 0.65, "mobile": 0.30, "tablet": 0.05},
                "search_engines":       ["google", "bing", "duckduckgo"],
                "keywords_file":        "data/keywords.json",
                "min_session_duration": 45,
                "max_session_duration": 480,
                "pages_per_session":    {"min": 1, "max": 8},
                "bounce_rate":          0.35,
                "schedule": {
                    "enabled":          False,
                    "peak_hours":       [9, 10, 11, 14, 15, 16, 20, 21],
                    "peak_multiplier":  2.0,
                    "weekend_factor":   0.7,
                },
            },
            "behavior": {
                "mouse_enabled":        True,
                "scroll_enabled":       True,
                "keyboard_enabled":     True,
                "typing_wpm_min":       35,
                "typing_wpm_max":       95,
                "scroll_speed":         "random",
                "click_delay_min":      0.08,
                "click_delay_max":      0.35,
                "attention_model":      True,
                "ml_adaptation":        True,
                "idle_probability":     0.08,
                "read_time_factor":     1.0,
            },
            "analytics": {
                "ga4_enabled":          False,
                "ga4_measurement_id":   "",
                "ga4_api_secret":       "",
                "pixel_enabled":        False,
                "pixel_id":             "",
                "heatmap_enabled":      False,
                "heatmap_provider":     "hotjar",
            },
            "security": {
                "captcha_service":      "2captcha",
                "captcha_api_key":      "",
                "captcha_budget":       10.0,
                "encryption_enabled":   False,
                "obfuscate_config":     False,
            },
            "monitoring": {
                "metrics_enabled":      True,
                "metrics_interval":     5.0,
                "self_healing":         True,
                "alert_email":          "",
                "webhook_url":          "",
                "dashboard_port":       8080,
                "performance_sampling": True,
            },
            "gui": {
                "enabled":              True,
                "theme":                "dark",
                "refresh_interval":     1.0,
                "chart_history":        300,
                "log_lines":            500,
                "window_width":         1400,
                "window_height":        900,
                "always_on_top":        False,
            },
        }