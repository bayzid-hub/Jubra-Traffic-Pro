import asyncio
import json
import hashlib
import os
import sys
import re
import ipaddress
import subprocess
import random
import shutil
import requests
import zipfile
import socket
import socketserver
import threading
import select
import time
from typing import Dict, Optional, Tuple
from urllib.parse import quote, unquote, urlparse

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.proxy import Proxy, ProxyType
from selenium_stealth import stealth
from fingerprint.dynamic_fingerprint import DynamicFingerprint
from behavior.advanced_biometrics_v3 import AdvancedBiometricsV3
from config import config
from utils.logger import get_logger
from database.db_manager import get_session, Profile

try:
    from fingerprint_factory.schema_adapter import adapt_fingerprint_schema
except Exception:  # keep launcher usable if factory adapter is unavailable
    adapt_fingerprint_schema = None

logger = get_logger(__name__)


_RUNTIME_CHROME_VERSION_CACHE = "runtime_chrome_version.json"


def _normalize_runtime_chrome_version(raw_text: str) -> Optional[str]:
    """Extract and validate a full Chrome/Chromium version string."""
    match = re.search(r"(\d+\.\d+\.\d+\.\d+)", str(raw_text or ""))
    if not match:
        return None
    version = match.group(1)
    try:
        major = int(version.split(".", 1)[0])
    except Exception:
        return None
    if 80 <= major <= 250:
        return version
    return None


def _runtime_chrome_version_cache_path() -> str:
    return os.path.join(str(config.BASE_DIR), "browser", _RUNTIME_CHROME_VERSION_CACHE)


def _write_runtime_chrome_version_cache(version: str, source: str = "launcher", executable_path: str = "") -> bool:
    """Persist the runtime Chrome version so profile generation can use source-of-truth."""
    normalized = _normalize_runtime_chrome_version(version)
    if not normalized:
        return False
    try:
        cache_path = _runtime_chrome_version_cache_path()
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        payload = {
            "version": normalized,
            "source": str(source or "launcher"),
            "executable_path": str(executable_path or ""),
            "updated_at": int(time.time()),
        }
        tmp_path = f"{cache_path}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
        os.replace(tmp_path, cache_path)
        return True
    except Exception as exc:
        logger.debug(f"Runtime Chrome version cache write skipped: {exc}")
        return False

class BrowserLauncher:
    def __init__(self):
        self.dynamic_fp = DynamicFingerprint()
        self.biometrics = AdvancedBiometricsV3()
        self._proxy_bridge_servers = []
        self._attached_chrome_processes = []
        self.time_engine = "BROWSER_ONLY"

        # Native OS timezone session state.
        # OS timezone is global on some platforms,, so it must be restored after browser close.
        self._native_tz_lock = threading.RLock()
        self._native_tz_original = None
        self._native_tz_sessions = {}
        
        self._resolve_chromium_paths()

    def _resolve_chromium_paths(self):
        """Resolve OS-specific Chromium and ChromeDriver paths.

        Each operating system uses its own native Chromium binary:
        - Windows: chrome.exe / chromedriver.exe
        - macOS:   Chromium.app/Contents/MacOS/Chromium / chromedriver
        - Linux:   chrome / chromedriver

        Bundled binaries are preferred; system-installed Chrome/Chromium
        is used as a fallback when the bundled binary is not found.
        """
        host_os = self._resolve_host_os_family()

        if host_os == "windows":
            chromium_name = "chrome.exe"
            driver_name = "chromedriver.exe"
        elif host_os == "macos":
            chromium_name = "Chromium.app/Contents/MacOS/Chromium"
            driver_name = "chromedriver"
        elif host_os == "linux":
            chromium_name = "chrome"
            driver_name = "chromedriver"
        else:
            chromium_name = "chrome"
            driver_name = "chromedriver"

        # Check environment variable overrides first
        env_chromium = os.environ.get("JUBRA_CHROMIUM_PATH", "").strip()
        env_driver = os.environ.get("JUBRA_CHROMEDRIVER_PATH", "").strip()

        chromium_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "chromium")
        self.chromium_path = os.path.join(chromium_dir, chromium_name)
        self.driver_path = os.path.join(chromium_dir, driver_name)

        # Apply environment variable overrides
        if env_chromium and os.path.isfile(env_chromium):
            self.chromium_path = env_chromium
            logger.info(f"Chromium path overridden by JUBRA_CHROMIUM_PATH: {env_chromium}")
        if env_driver and os.path.isfile(env_driver):
            self.driver_path = env_driver
            logger.info(f"ChromeDriver path overridden by JUBRA_CHROMEDRIVER_PATH: {env_driver}")

        # Fallback to system-installed Chrome/Chromium if bundled not found
        if not os.path.exists(self.chromium_path):
            system_fallbacks = {
                "windows": [
                    os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
                    os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
                    os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
                ],
                "macos": [
                    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                    "/Applications/Chromium.app/Contents/MacOS/Chromium",
                    os.path.expanduser("~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
                    os.path.expanduser("~/Applications/Chromium.app/Contents/MacOS/Chromium"),
                ],
                "linux": [
                    "/usr/bin/google-chrome",
                    "/usr/bin/google-chrome-stable",
                    "/usr/bin/chromium-browser",
                    "/usr/bin/chromium",
                    "/snap/bin/chromium",
                ],
            }
            for fallback_path in system_fallbacks.get(host_os, []):
                if os.path.exists(fallback_path):
                    self.chromium_path = fallback_path
                    logger.info(f"Bundled Chromium not found; using system Chrome: {fallback_path}")
                    break
            else:
                logger.warning(
                    f"No Chromium/Chrome binary found for {host_os}. "
                    f"Searched: {self.chromium_path} and system paths. "
                    "Set JUBRA_CHROMIUM_PATH environment variable to specify the path."
                )

        if not os.path.exists(self.driver_path):
            logger.warning(
                f"ChromeDriver not found at: {self.driver_path}. "
                "Set JUBRA_CHROMEDRIVER_PATH environment variable to specify the path."
            )

        logger.info(
            f"Chromium paths resolved for {host_os}: "
            f"browser={self.chromium_path} | driver={self.driver_path}"
        )

    def _diag_enabled(self, name: str) -> bool:
        """Return True when a diagnostic environment flag is enabled."""
        value = os.environ.get(str(name or ""), "")
        return str(value).strip().lower() in {"1", "true", "yes", "on"}

    def _resolve_diagnostic_flags(self) -> Dict[str, bool]:
        """Resolve temporary PixelScan layer-isolation flags.

        These flags are for debugging only. They let us turn one layer off at a time
        so the PixelScan red signal can be mapped to a specific launcher layer.
        """
        pure = self._diag_enabled("JUBRA_DIAG_PURE")
        flags = {
            "pure": pure,
            "no_screen_args": pure or self._diag_enabled("JUBRA_DIAG_NO_SCREEN_ARGS"),
            "no_ua_arg": pure or self._diag_enabled("JUBRA_DIAG_NO_UA_ARG"),
            "no_lang_arg": pure or self._diag_enabled("JUBRA_DIAG_NO_LANG_ARG"),
            "no_cdp_identity": pure or self._diag_enabled("JUBRA_DIAG_NO_CDP_IDENTITY"),
            "no_stealth": pure or self._diag_enabled("JUBRA_DIAG_NO_STEALTH"),
            "no_advanced_cdp": pure or self._diag_enabled("JUBRA_DIAG_NO_ADVANCED_CDP"),
            "no_custom_js": pure or self._diag_enabled("JUBRA_DIAG_NO_CUSTOM_JS"),
            "no_timezone_cdp": pure or self._diag_enabled("JUBRA_DIAG_NO_TIMEZONE_CDP"),
            "no_extra_js": pure or self._diag_enabled("JUBRA_DIAG_NO_EXTRA_JS"),
        }
        enabled = [name for name, enabled in flags.items() if enabled]
        if enabled:
            logger.warning("DIAGNOSTIC LAYER MODE ACTIVE: " + ", ".join(enabled))
        return flags

    def _chrome_options_arguments(self, options) -> list:
        """Return Chrome launch arguments from Selenium Options without using ChromeDriver to spawn Chrome."""
        try:
            values = getattr(options, "arguments", None)
            if values is None:
                values = getattr(options, "_arguments", [])
            return [str(item) for item in (values or []) if str(item).strip()]
        except Exception:
            return []

    def _get_free_loopback_port(self) -> int:
        """Reserve a free local port for Chrome remote-debug attach mode."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])
        finally:
            try:
                sock.close()
            except Exception:
                pass

    def _wait_for_remote_debug_port(self, port: int, timeout: float = 20.0) -> bool:
        """Wait until Chrome's remote debugging endpoint is reachable."""
        deadline = time.time() + float(timeout)
        last_error = ""

        while time.time() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", int(port)), timeout=1.0):
                    try:
                        response = requests.get(
                            f"http://127.0.0.1:{int(port)}/json/version",
                            timeout=2.0,
                        )
                        if response.status_code == 200:
                            logger.info(
                                f"Remote debugging port ready: 127.0.0.1:{int(port)}"
                            )
                            return True
                    except Exception:
                        logger.info(
                            f"Remote debugging socket ready: 127.0.0.1:{int(port)}"
                        )
                        return True
            except Exception as exc:
                last_error = str(exc)
                time.sleep(0.25)

        raise RuntimeError(
            f"Remote debugging port did not become ready: 127.0.0.1:{int(port)} | {last_error}"
        )

    def _deep_merge_dict(self, base: dict, updates: dict) -> dict:
        """Merge nested dictionaries without replacing unrelated Chrome preferences."""
        if not isinstance(base, dict):
            base = {}
        if not isinstance(updates, dict):
            return base
        for key, value in updates.items():
            if isinstance(value, dict) and isinstance(base.get(key), dict):
                self._deep_merge_dict(base[key], value)
            else:
                base[key] = value
        return base

    def _google_search_provider_launch_arguments(self) -> list:
        """Return Chromium launch switches that seed omnibox search on first profile start.

        Portable Chromium builds may start with a "No Search" provider until the
        Web Data database is created. These switches are scoped only to the
        default search provider and do not touch fingerprint, proxy, timezone or
        stealth layers.
        """
        return [
            "--disable-search-engine-choice-screen",
            "--search-provider-name=Google",
            "--search-provider-keyword=google.com",
            "--search-provider-search-url=https://www.google.com/search?q={searchTerms}",
            "--search-provider-suggest-url=https://www.google.com/complete/search?client=chrome&q={searchTerms}",
            "--search-provider-encoding=UTF-8",
        ]

    def _prepare_chrome_profile_baseline_preferences(self, profile_dir: str, languages: list = None, proxy_enabled: bool = False) -> bool:
        """Write launch-critical Chrome preferences before subprocess attach mode starts.

        Selenium Options experimental prefs are only applied when ChromeDriver starts
        Chrome directly. In remote-debug attach mode we start Chrome with subprocess,
        so preferences such as default search provider, popup policy and language must
        be written into the profile's Preferences file before Chrome starts.
        """
        try:
            profile_dir = os.path.abspath(str(profile_dir or ""))
            if not profile_dir:
                return False

            default_dir = os.path.join(profile_dir, "Default")
            os.makedirs(default_dir, exist_ok=True)
            preferences_path = os.path.join(default_dir, "Preferences")

            preferences = {}
            if os.path.exists(preferences_path):
                try:
                    with open(preferences_path, "r", encoding="utf-8") as fh:
                        loaded = json.load(fh)
                    if isinstance(loaded, dict):
                        preferences = loaded
                except Exception as exc:
                    logger.warning(f"Existing Chrome Preferences read skipped: {exc}")

            language_values = [str(item).strip() for item in (languages or []) if str(item).strip()]
            if not language_values:
                language_values = ["en-US", "en"]

            baseline = {
                "intl": {
                    "accept_languages": ",".join(language_values[:4]),
                },
                "default_search_provider": {
                    "enabled": True,
                    "name": "Google",
                    "keyword": "google.com",
                    "search_url": "https://www.google.com/search?q={searchTerms}",
                    "suggest_url": "https://www.google.com/complete/search?client=chrome&q={searchTerms}",
                    "icon_url": "https://www.google.com/favicon.ico",
                    "encoding": "UTF-8",
                    "prepopulate_id": 1,
                    "id": 1,
                    "alternate_urls": [],
                    "search_terms_replacement_key": "{searchTerms}",
                },
                "default_search_provider_data": {
                    "template_url_data": {
                        "short_name": "Google",
                        "keyword": "google.com",
                        "url": "https://www.google.com/search?q={searchTerms}",
                        "suggestions_url": "https://www.google.com/complete/search?client=chrome&q={searchTerms}",
                        "favicon_url": "https://www.google.com/favicon.ico",
                        "input_encodings": ["UTF-8"],
                        "prepopulate_id": 1,
                        "safe_for_autoreplace": True,
                    }
                },
                "omnibox": {
                    "keyword_search_button": True,
                    "prevent_url_elisions": False,
                },
                "profile": {
                    "default_content_setting_values": {
                        # Internal verifier opens a temporary verification tab. Allowing
                        # popups at the profile level prevents Chrome's popup blocker from
                        # blocking that controlled diagnostic tab.
                        "popups": 1,
                    },
                    "exit_type": "Normal",
                    "exited_cleanly": True,
                },
                "browser": {
                    "has_seen_welcome_page": True,
                    "check_default_browser": False,
                },
            }

            if proxy_enabled:
                self._deep_merge_dict(baseline, {
                    "dns_over_https": {
                        # FIX #8: DoH mode must be "secure" (not "off") to prevent
                        # DNS queries from leaking to the ISP's DNS servers.
                        # "secure" forces all DNS resolution through DoH,
                        # preventing ISP DNS leak when proxy is active.
                        "mode": "secure",
                        "templates": "https://dns.google/dns-query https://cloudflare-dns.com/dns-query",
                    },
                    "net": {
                        "network_prediction_options": 2,
                    },
                })

            self._deep_merge_dict(preferences, baseline)

            tmp_path = f"{preferences_path}.tmp"
            with open(tmp_path, "w", encoding="utf-8") as fh:
                json.dump(preferences, fh, ensure_ascii=False, separators=(",", ":"))
            os.replace(tmp_path, preferences_path)

            logger.info(
                "Chrome profile baseline preferences prepared before launch: "
                f"languages={','.join(language_values[:4])} | default_search_provider=Google | popups=allow"
            )
            return True

        except Exception as exc:
            logger.warning(f"Chrome profile baseline preferences prepare skipped: {exc}")
            return False

    def _prepare_google_search_provider_web_data(self, profile_dir: str) -> bool:
        """Convert Chromium's built-in No Search provider row to Google.

        Root cause from runtime diagnostics: this portable Chromium build creates
        Default/Web Data with prepopulate_id=1 as "No Search" and URL
        http://{searchTerms}. Chromium then treats plain text in the address bar
        as a URL. Do not delete/recreate the default row; update that same row so
        Chromium's internal default-provider mapping keeps pointing to a valid
        Google template.
        """
        try:
            import sqlite3

            profile_dir = os.path.abspath(str(profile_dir or ""))
            web_data_path = os.path.join(profile_dir, "Default", "Web Data")
            if not os.path.exists(web_data_path):
                logger.info("Chrome Web Data search provider update skipped; database not created yet.")
                return False

            conn = sqlite3.connect(web_data_path, timeout=10)
            try:
                cur = conn.cursor()
                tables = {row[0] for row in cur.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if "keywords" not in tables or "meta" not in tables:
                    logger.info("Chrome Web Data search provider update skipped; expected tables are missing.")
                    return False

                keyword_columns = [row[1] for row in cur.execute("PRAGMA table_info(keywords)")]
                if not keyword_columns or "id" not in keyword_columns:
                    logger.info("Chrome Web Data search provider update skipped; keywords schema has no id column.")
                    return False

                target_row = cur.execute(
                    """
                    SELECT id, short_name, keyword, url, prepopulate_id
                    FROM keywords
                    WHERE lower(keyword) IN ('nosearch', 'google.com', 'google')
                       OR lower(short_name) IN ('no search', 'google')
                       OR prepopulate_id = 1
                    ORDER BY CASE
                        WHEN lower(keyword) = 'nosearch' OR lower(short_name) = 'no search' THEN 0
                        WHEN prepopulate_id = 1 THEN 1
                        WHEN lower(keyword) IN ('google.com', 'google') OR lower(short_name) = 'google' THEN 2
                        ELSE 3
                    END, id
                    LIMIT 1
                    """
                ).fetchone()

                if target_row:
                    target_id = int(target_row[0])
                    original_short_name = str(target_row[1] or "")
                    original_keyword = str(target_row[2] or "")
                    action = "converted" if (
                        original_short_name.lower() == "no search" or original_keyword.lower() == "nosearch"
                    ) else "updated"
                else:
                    row = cur.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM keywords").fetchone()
                    target_id = int(row[0] or 1)
                    action = "inserted"

                now_chrome_time = int((time.time() + 11644473600) * 1000000)
                values = {
                    "id": target_id,
                    "short_name": "Google",
                    "keyword": "google.com",
                    "favicon_url": "https://www.google.com/favicon.ico",
                    "url": "https://www.google.com/search?q={searchTerms}",
                    "safe_for_autoreplace": 1,
                    "originating_url": "",
                    "date_created": now_chrome_time,
                    "usage_count": 0,
                    "input_encodings": "UTF-8",
                    "suggest_url": "https://www.google.com/complete/search?client=chrome&q={searchTerms}",
                    "prepopulate_id": 1,
                    "created_by_policy": 0,
                    "last_modified": now_chrome_time,
                    "sync_guid": "jubra-google-default-search",
                    "alternate_urls": "[]",
                    "image_url": "",
                    "search_url_post_params": "",
                    "suggest_url_post_params": "",
                    "image_url_post_params": "",
                    "new_tab_url": "",
                    "last_visited": 0,
                    "created_from_play_api": 0,
                    "is_active": 0,
                    "starter_pack_id": 0,
                    "enforced_by_policy": 0,
                    "featured_by_policy": 0,
                }

                # Remove duplicate Google/No Search rows but preserve the row Chromium
                # already mapped as the default provider.
                cur.execute(
                    """
                    DELETE FROM keywords
                    WHERE id != ?
                      AND (
                        lower(keyword) IN ('google.com', 'google', 'nosearch')
                        OR lower(short_name) IN ('google', 'no search')
                      )
                    """,
                    (target_id,),
                )

                if action == "inserted":
                    insert_columns = [col for col in keyword_columns if col in values]
                    placeholders = ",".join(["?"] * len(insert_columns))
                    cur.execute(
                        f"INSERT INTO keywords ({','.join(insert_columns)}) VALUES ({placeholders})",
                        [values[col] for col in insert_columns],
                    )
                else:
                    update_columns = [col for col in keyword_columns if col in values and col != "id"]
                    set_clause = ",".join([f"{col}=?" for col in update_columns])
                    cur.execute(
                        f"UPDATE keywords SET {set_clause} WHERE id=?",
                        [values[col] for col in update_columns] + [target_id],
                    )

                cur.execute(
                    "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                    ("Default Search Provider ID", str(target_id)),
                )
                cur.execute(
                    "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                    ("Default Search Provider GUID", "jubra-google-default-search"),
                )
                conn.commit()

                logger.info(
                    "Chrome Web Data No Search provider converted to Google: "
                    f"keyword_id={target_id} | action={action}"
                )
                return True
            finally:
                try:
                    conn.close()
                except Exception:
                    pass

        except Exception as exc:
            logger.warning(f"Chrome Web Data search provider prepare skipped: {exc}")
            return False

    def _chrome_web_data_google_provider_ready(self, profile_dir: str) -> bool:
        """Return True only when Web Data already contains a usable Google row."""
        try:
            import sqlite3

            profile_dir = os.path.abspath(str(profile_dir or ""))
            web_data_path = os.path.join(profile_dir, "Default", "Web Data")
            if not os.path.exists(web_data_path):
                return False

            conn = sqlite3.connect(web_data_path, timeout=5)
            try:
                cur = conn.cursor()
                row = cur.execute(
                    """
                    SELECT id FROM keywords
                    WHERE lower(short_name) = 'google'
                      AND lower(keyword) = 'google.com'
                      AND url = 'https://www.google.com/search?q={searchTerms}'
                    ORDER BY id
                    LIMIT 1
                    """
                ).fetchone()
                nosearch_row = cur.execute(
                    """
                    SELECT id FROM keywords
                    WHERE lower(short_name) = 'no search'
                       OR lower(keyword) = 'nosearch'
                       OR url = 'http://{searchTerms}'
                    LIMIT 1
                    """
                ).fetchone()
                return bool(row and not nosearch_row)
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
        except Exception:
            return False

    def _bootstrap_chrome_web_data_for_search_provider(self, profile_dir: str) -> bool:
        """One-time first-profile-start bootstrap for portable Chromium search.

        Fresh profiles do not have Default/Web Data until Chromium starts once.
        Without this bootstrap the first visible launch can still use the bundled
        "No Search" provider. This starts Chromium briefly off-screen only to let
        it create Web Data, closes it, converts the built-in No Search row to
        Google, then the normal visible launch continues.
        """
        try:
            profile_dir = os.path.abspath(str(profile_dir or ""))
            default_dir = os.path.join(profile_dir, "Default")
            web_data_path = os.path.join(default_dir, "Web Data")

            if self._chrome_web_data_google_provider_ready(profile_dir):
                logger.info("Chrome Web Data Google search provider already ready before launch.")
                return True

            if os.path.exists(web_data_path):
                return self._prepare_google_search_provider_web_data(profile_dir)

            os.makedirs(default_dir, exist_ok=True)

            command = [
                self.chromium_path,
                f"--user-data-dir={profile_dir}",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-search-engine-choice-screen",
                "--disable-extensions",
                "--disable-background-mode",
                "--window-size=800,600",
                "--window-position=-32000,-32000",
            ] + self._google_search_provider_launch_arguments() + ["about:blank"]

            creationflags = 0
            if os.name == "nt":
                creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                creationflags |= getattr(subprocess, "CREATE_NO_WINDOW", 0)

            proc = None
            try:
                logger.info("Chrome Web Data missing; running one-time off-screen search provider bootstrap.")
                proc = subprocess.Popen(
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=creationflags,
                )

                deadline = time.time() + 12.0
                while time.time() < deadline:
                    if os.path.exists(web_data_path):
                        break
                    if proc.poll() is not None:
                        break
                    time.sleep(0.25)
            finally:
                if proc and proc.poll() is None:
                    try:
                        proc.terminate()
                        proc.wait(timeout=5)
                    except Exception:
                        try:
                            proc.kill()
                        except Exception:
                            pass
                time.sleep(0.75)

            prepared = self._prepare_google_search_provider_web_data(profile_dir)
            if prepared:
                logger.info("Chrome Web Data search provider bootstrap completed before visible launch.")
            else:
                logger.warning("Chrome Web Data search provider bootstrap did not complete; Web Data still unavailable.")
            return bool(prepared)

        except Exception as exc:
            logger.warning(f"Chrome Web Data first-run bootstrap skipped: {exc}")
            return False

    def _decode_omnibox_host_query(self, current_url: str) -> Optional[str]:
        """Return a Google-search query when Chromium treats plain text as a URL.

        Some portable Chromium builds can stay in a "No Search" default-provider
        state even after Preferences/Web Data are seeded. In that state, typing a
        plain query such as "hello test" navigates to http://hello%20test instead
        of using the default search provider. This helper detects only that
        browser-core failure shape; it does not touch fingerprint, proxy,
        timezone, UA, stealth or WebGL layers.
        """
        try:
            raw_url = str(current_url or "").strip()
            if not raw_url:
                return None

            parsed = urlparse(raw_url)
            if parsed.scheme not in {"http", "https"}:
                return None

            host = str(parsed.netloc or "").strip().lower()
            if not host:
                return None

            # Never intercept real hosts, local developer addresses, IPs, or domains.
            if host in {"localhost", "127.0.0.1", "::1"}:
                return None
            try:
                ipaddress.ip_address(host.strip("[]"))
                return None
            except Exception:
                pass
            if "." in host or ":" in host:
                return None

            decoded = unquote(host).strip()
            if not decoded:
                return None
            if decoded.lower() in {"localhost", "about", "chrome", "file"}:
                return None

            # The clearest broken-DSE case is http://hello%20test. Also handle a
            # single bare word because Chromium's "No Search" mode navigates it as
            # http://word/ instead of searching.
            if "%" in host or " " in decoded or re.match(r"^[a-z0-9][a-z0-9\-]{1,80}$", decoded, re.I):
                query = decoded.replace("+", " ").strip()
                query = re.sub(r"\s+", " ", query)
                if query:
                    return query
        except Exception:
            return None
        return None

    def _start_omnibox_search_fallback_controller(self, driver) -> None:
        """Redirect broken plain-text omnibox URL attempts to Google search.

        This is a narrow safety net for portable Chromium profiles stuck in
        "No Search" mode. It runs only after the browser is launched and only
        when the current tab navigates to a non-domain URL like
        http://hello%20test. It preserves all existing fingerprint/proxy layers.
        """
        try:
            if driver is None:
                return
            driver_key = id(driver)
            if not hasattr(self, "_omnibox_search_fallback_threads"):
                self._omnibox_search_fallback_threads = set()
            if driver_key in self._omnibox_search_fallback_threads:
                return
            self._omnibox_search_fallback_threads.add(driver_key)

            def _worker():
                seen = set()
                while True:
                    try:
                        current_url = str(getattr(driver, "current_url", "") or "")
                        query = self._decode_omnibox_host_query(current_url)
                        if query and current_url not in seen:
                            seen.add(current_url)
                            target_url = "https://www.google.com/search?q=" + quote(query, safe="")
                            logger.info(
                                "Omnibox search fallback redirected plain text query to Google search: "
                                f"query={query}"
                            )
                            try:
                                driver.execute_script("window.location.replace(arguments[0]);", target_url)
                            except Exception:
                                driver.get(target_url)
                        time.sleep(0.35)
                    except Exception:
                        break

            thread = threading.Thread(
                target=_worker,
                name=f"JubraOmniboxSearchFallback-{driver_key}",
                daemon=True,
            )
            thread.start()
            logger.info("Omnibox search fallback controller active for this browser session.")
        except Exception as exc:
            logger.warning(f"Omnibox search fallback controller skipped: {exc}")

    def _launch_chrome_subprocess_and_attach(self, options) -> object:
        """Launch Chrome like a normal/manual browser, then attach Selenium to it.

        This avoids ChromeDriver being the process that spawns Chromium directly.
        It keeps the manual portable-Chromium behavior closer to the green
        PixelScan reference while preserving Selenium control after launch.
        """
        debug_port = self._get_free_loopback_port()
        chrome_binary = str(getattr(options, "binary_location", "") or self.chromium_path)

        chrome_args = []
        seen_args = set()

        for arg in self._chrome_options_arguments(options):
            arg = str(arg).strip()
            if not arg:
                continue

            if arg.startswith("--remote-debugging-port"):
                continue

            if arg == "--remote-allow-origins=*":
                continue

            if not self._diag_enabled("JUBRA_LEGACY_CHROME_FLAGS"):
                blocked_exact = {
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-background-networking",
                    "--disable-breakpad",
                    "--disable-component-update",
                    "--disable-default-apps",
                    "--disable-sync",
                    "--enable-accelerated-2d-canvas",
                    "--enable-gpu-rasterization",
                    "--ignore-gpu-blocklist",
                }
                blocked_prefixes = (
                    "--disable-features=IsolateOrigins",
                    "--disable-features=site-per-process",
                )
                # Allow --disable-blink-features=AutomationControlled through
                # the native-clean filter. This flag is critical for hiding
                # navigator.webdriver; blocking it causes PixelScan and other
                # checkers to detect automation.
                is_allowed_automation_flag = (
                    arg == "--disable-blink-features=AutomationControlled"
                )
                if is_allowed_automation_flag:
                    pass  # Allow this specific automation-hiding flag
                elif arg in blocked_exact or any(arg.startswith(prefix) for prefix in blocked_prefixes):
                    # Also block other --disable-blink-features= values that
                    # are not the specific AutomationControlled flag.
                    if arg.startswith("--disable-blink-features=") and arg != "--disable-blink-features=AutomationControlled":
                        logger.info(f"Native-clean attach mode filtered Chrome flag: {arg}")
                        continue
                    logger.info(f"Native-clean attach mode filtered Chrome flag: {arg}")
                    continue

            if arg not in seen_args:
                chrome_args.append(arg)
                seen_args.add(arg)

        chrome_args.append(f"--remote-debugging-port={int(debug_port)}")
        chrome_args.append("--remote-allow-origins=*")
        chrome_args.append("about:blank")

        command = [chrome_binary] + chrome_args

        creationflags = 0
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

        chrome_process = None
        try:
            chrome_process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=creationflags,
            )
            self._attached_chrome_processes.append(chrome_process)

            logger.info(
                "Chrome launched by subprocess in native/manual mode: "
                f"pid={chrome_process.pid} | debug_port={debug_port}"
            )

            self._wait_for_remote_debug_port(debug_port, timeout=25.0)

            attach_options = Options()
            attach_options.binary_location = chrome_binary
            attach_options.add_experimental_option(
                "debuggerAddress",
                f"127.0.0.1:{int(debug_port)}",
            )

            service = Service(executable_path=self.driver_path)
            driver = webdriver.Chrome(service=service, options=attach_options)

            logger.info(
                "Selenium attached to existing browser session; "
                "ChromeDriver direct browser launch skipped."
            )
            return driver

        except Exception:
            try:
                if chrome_process and chrome_process.poll() is None:
                    chrome_process.terminate()
            except Exception:
                pass
            raise

    def _normalize_os_family(self, value: str) -> Optional[str]:
        text = str(value or "").strip().lower()
        if not text:
            return None
        # Mobile families are checked first: Android UAs contain "linux" and iOS UAs
        # contain "mac os x", so ordering avoids misclassifying them as desktop OSes.
        if "android" in text:
            return "android"
        if any(token in text for token in ("iphone", "ipad", "ipod")) or text == "ios" or "iphone os" in text:
            return "ios"
        if any(token in text for token in ("macos", "mac os", "macintosh", "darwin", "osx", "macintel")):
            return "macos"
        if any(token in text for token in ("linux", "x11")):
            return "linux"
        if any(token in text for token in ("windows", "win32", "win64", "win")):
            return "windows"
        return None

    def _resolve_host_os_family(self) -> Optional[str]:
        return self._normalize_os_family(sys.platform) or self._normalize_os_family(os.name)

    def _resolve_runtime_os_family(self) -> Optional[str]:
        browser_path = str(self.chromium_path or "").strip().lower()
        if browser_path.endswith(".exe"):
            return "windows"
        if ".app/" in browser_path or browser_path.endswith(".app"):
            return "macos"
        return self._resolve_host_os_family()

    def _resolve_fingerprint_os_family(self, fingerprint: Dict) -> Optional[str]:
        if not isinstance(fingerprint, dict):
            return None

        for key in ("os", "os_type", "platform_os", "platform"):
            os_family = self._normalize_os_family(fingerprint.get(key))
            if os_family:
                return os_family

        user_agent = fingerprint.get("userAgent") or fingerprint.get("user_agent")
        return self._normalize_os_family(user_agent)

    def _enforce_runtime_os_guard(self, fingerprint: Dict):
        runtime_os = self._resolve_runtime_os_family()
        profile_os = self._resolve_fingerprint_os_family(fingerprint)

        if not runtime_os or not profile_os:
            logger.warning(
                "OS runtime guard could not verify profile/runtime OS; launch continues. "
                f"runtime_os={runtime_os or 'unknown'} | profile_os={profile_os or 'unknown'}"
            )
            return

        if runtime_os != profile_os:
            # Emulation/spoofing mode: cross-OS profiles are allowed. The requested OS
            # identity is applied through user-agent, client-hints and (for Android/iOS)
            # device-metrics emulation on the native Chromium runtime instead of blocking
            # the launch.
            logger.info(
                "OS emulation mode active: "
                f"profile_os={profile_os} | runtime_os={runtime_os}. "
                "Applying cross-OS identity via emulation instead of blocking launch."
            )
            return

        logger.info(
            "OS runtime guard passed: "
            f"profile_os={profile_os} | runtime_os={runtime_os}"
        )

    def _resolve_canonical_launch_fingerprint(self, profile: Dict, domain: str = None) -> Dict:
        """Return the in-memory canonical fingerprint bundle consumed by launcher.

        Step 5 goal: launcher consumes the saved canonical profile fingerprint when
        it exists. It should not regenerate or silently reshape identities for
        valid saved profiles. The old dynamic fallback remains only as an
        emergency path for missing/invalid legacy profiles.
        """
        raw_fingerprint = profile.get("fingerprint") if isinstance(profile, dict) else None

        if isinstance(raw_fingerprint, dict) and raw_fingerprint:
            try:
                # Deep-copy through JSON so launcher runtime mutations never alter the
                # profile dict object fetched from DB/UI memory. Persistence remains
                # controlled by explicit existing save paths only.
                launch_fingerprint = json.loads(json.dumps(raw_fingerprint))
            except Exception:
                launch_fingerprint = dict(raw_fingerprint)

            if adapt_fingerprint_schema:
                try:
                    launch_fingerprint = adapt_fingerprint_schema(launch_fingerprint)
                except Exception as exc:
                    logger.warning(
                        "Canonical fingerprint schema adapter skipped in launcher; "
                        f"using saved fingerprint as-is: {exc}"
                    )

            launch_fingerprint.setdefault(
                "launcher_consumer_version",
                "launcher_canonical_consumer_v1",
            )

            logger.info(
                "AUDIT: launcher_canonical_fingerprint_consumed | "
                f"schema={launch_fingerprint.get('fingerprint_schema_version', 'unknown')} | "
                f"factory={launch_fingerprint.get('factory_version', 'unknown')} | "
                f"hash={str(launch_fingerprint.get('fingerprint_hash') or '')[:12] or 'missing'}"
            )
            logger.info(
                "Launcher consuming saved canonical fingerprint: "
                f"schema={launch_fingerprint.get('fingerprint_schema_version', 'unknown')} | "
                f"factory={launch_fingerprint.get('factory_version', 'unknown')} | "
                f"hash={str(launch_fingerprint.get('fingerprint_hash') or '')[:12] or 'missing'}"
            )
            return launch_fingerprint

        logger.warning(
            "LOCK WARNING: launcher_dynamic_fingerprint_emergency_fallback_triggered | "
            "saved canonical fingerprint missing or invalid | legacy dynamic fallback approved for emergency launch only"
        )
        logger.warning(
            "Profile has no saved fingerprint. Using legacy emergency dynamic "
            "fingerprint fallback for launch only."
        )
        fallback = self.dynamic_fp.get_session_fingerprint(domain, risk_level=0.3)
        if not isinstance(fallback, dict):
            fallback = {}
        if adapt_fingerprint_schema:
            try:
                fallback = adapt_fingerprint_schema(fallback)
            except Exception as exc:
                logger.warning(f"Legacy fallback schema adaptation skipped: {exc}")
        fallback.setdefault("launcher_consumer_version", "launcher_legacy_emergency_fallback_v1")
        return fallback
    
    async def launch_profile(self, profile_id: int, headless: bool = False):
        if not os.path.exists(self.chromium_path):
            logger.info("Portable Chromium not found. Downloading automatically...")
            self.update_chromium()
        
        return await asyncio.to_thread(self._launch_profile_sync, profile_id, headless)
    
    def _launch_profile_sync(self, profile_id: int, headless: bool = False):
        session = get_session()
        try:
            profile = session.query(Profile).filter_by(id=profile_id).first()
            if not profile:
                logger.error(f"Profile with ID {profile_id} not found.")
                return None, None, None
            
            profile_dict = {
                'id': profile.id,
                'name': profile.name,
                'fingerprint': profile.fingerprint or {},
                'proxy_config': profile.proxy_config or {},
                'health_score': profile.health_score,
                'status': profile.status
            }
            logger.info(f"Launching browser for profile: {profile.name}")
            return self._launch_sync(profile_dict, headless=headless)
        finally:
            session.close()
    
    def _launch_sync(self, profile: Dict, domain: str = None, headless: bool = False) -> Tuple[object, object, object]:
        browser_timezone_session = None

        try:
            fingerprint = self._resolve_canonical_launch_fingerprint(profile, domain=domain)
            self._enforce_runtime_os_guard(fingerprint)
            diag_flags = self._resolve_diagnostic_flags()
            
            proxy_config = profile.get('proxy_config', {})
            proxy_chain = None
            if proxy_config and isinstance(proxy_config, dict) and proxy_config.get('proxy'):
                proxy_chain = [proxy_config['proxy']]
            elif proxy_config and isinstance(proxy_config, str):
                proxy_chain = [proxy_config]
            
            proxy_info = None
            proxy_auth_required = False
            if proxy_chain:
                proxy_info = self._parse_proxy_config(proxy_chain[0])
                if proxy_info:
                    proxy_auth_required = bool(proxy_info.get("username") or proxy_info.get("password"))
                else:
                    logger.warning("Invalid proxy configuration. Launching without proxy.")

            if proxy_info:
                # Browser-only mode:
                # Resolve proxy timezone before Chrome starts, but do NOT change Windows timezone.
                browser_timezone_session = self._prepare_browser_proxy_timezone(
                    proxy_info=proxy_info,
                    fingerprint=fingerprint,
                )
            
            logger.info(f"Launching browser for profile: {profile.get('name', 'unknown')}")
            
            screen = fingerprint.get('screen', {})
            width = screen.get('width', 1920)
            height = screen.get('height', 1080)
            launch_window = self._resolve_browser_launch_window(fingerprint)
            
            options = Options()
            options.add_experimental_option("excludeSwitches", ["enable-automation"])
            options.add_experimental_option('useAutomationExtension', False)

            # Privacy: Hide navigator.webdriver via CDP.
            # This is applied BEFORE any page loads so no website can detect
            # automation. Combined with excludeSwitches above and __jubraKit
            # injection later, this provides triple-layer webdriver hiding.
            options.add_argument('--disable-blink-features=AutomationControlled')

            if not os.path.exists(self.chromium_path):
                raise FileNotFoundError("Portable Chromium still missing. Please run 'Update Chromium' from Settings.")
            options.binary_location = self.chromium_path
            
            profile_id = profile.get('id', 'default')
            storage_policy = self._resolve_profile_storage_policy(fingerprint)
            storage_mode = storage_policy.get("mode", "persistent")

            profiles_root = os.path.abspath(
                os.path.join(str(config.BASE_DIR), "browser_profiles")
            )
            os.makedirs(profiles_root, exist_ok=True)

            storage_identity = storage_policy.get("storage_id") or profile_id

            if storage_mode == "fresh":
                launch_token = f"{int(time.time())}_{random.randint(100000, 999999)}"
                profile_dir = os.path.join(
                    profiles_root,
                    f"{self._safe_profile_storage_dir_name(storage_identity)}_{launch_token}"
                )
                storage_label = "fresh temporary"
            else:
                profile_dir = os.path.join(
                    profiles_root,
                    self._safe_profile_storage_dir_name(storage_identity),
                )
                storage_label = "persistent"

            os.makedirs(profile_dir, exist_ok=True)

            options.add_argument(f'--user-data-dir={profile_dir}')
            logger.info(
                f"Using {storage_label} browser profile dir: {profile_dir} | "
                f"cookies={storage_policy.get('cookies')} | "
                f"localStorage={storage_policy.get('localStorage')}"
            )
            
            options.add_argument('--no-first-run')
            options.add_argument('--no-default-browser-check')

            # Native-clean launch hygiene:
            # The manual portable Chromium test passed PixelScan, while the app launch showed
            # an unsupported command-line flag banner. Keep the launch command close to a
            # normal/manual Chrome start and avoid Linux/container/automation-style flags on Windows.
            if self._diag_enabled("JUBRA_LEGACY_CHROME_FLAGS"):
                logger.warning(
                    "Legacy Chrome launch flags enabled by JUBRA_LEGACY_CHROME_FLAGS=1. "
                    "This may show unsupported command-line warnings and change browser surfaces."
                )
                options.add_argument('--disable-default-apps')
                options.add_argument('--disable-sync')
                options.add_argument('--disable-background-networking')
                options.add_argument('--disable-breakpad')
                options.add_argument('--disable-component-update')
                if not proxy_auth_required:
                    options.add_argument('--disable-extensions')
                options.add_argument('--disable-blink-features=AutomationControlled')
                options.add_argument('--disable-features=IsolateOrigins,site-per-process')
                options.add_argument('--no-sandbox')
                options.add_argument('--disable-setuid-sandbox')
                options.add_argument('--disable-dev-shm-usage')
                options.add_argument('--enable-accelerated-2d-canvas')
                options.add_argument('--enable-gpu-rasterization')
                options.add_argument('--ignore-gpu-blocklist')
            else:
                logger.info(
                    "Native-clean Chrome launch hygiene active; unsupported sandbox, "
                    "container, extension-disable, and AutomationControlled flags skipped."
                )

            # Extension isolation must still be active in native-clean mode. The manual
            # Chromium test with --disable-extensions stayed green and prevented IDM/system
            # external extensions from leaking into privacy profiles. For authenticated
            # proxy profiles, the proxy auth extension is loaded later with an allow-list.
            if proxy_auth_required:
                logger.info(
                    "Extension isolation active: proxy auth extension allow-list mode will be used; "
                    "external/system Chrome extensions remain blocked."
                )
            else:
                options.add_argument('--disable-extensions')
                logger.info(
                    "Extension isolation active: external/system Chrome extensions disabled for this profile."
                )

            # WebRTC and network privacy protection for ALL profiles.
            # A privacy browser MUST prevent WebRTC IP leaks even in direct/no-proxy mode.
            # Without this, any website can discover the user's real IP and local network
            # topology via STUN/ICE requests.
            #
        # Policy: disable_non_proxied_udp - blocks non-proxied UDP which prevents

            # WebRTC from revealing real public IP and local IP addresses.
            options.add_argument('--force-webrtc-ip-handling-policy=disable_non_proxied_udp')
            options.add_argument('--enable-features=WebRtcHideLocalIpsWithMdns')

            if proxy_info:
                options.add_argument('--disable-quic')
                options.add_argument('--dns-prefetch-disable')
                options.add_argument('--disable-features=UseDnsHttpsSvcbAlpn')
                logger.info("Proxy network hardening active: QUIC disabled, DNS prefetch disabled, secure-DNS helper disabled.")
            else:
                # Direct profiles still get DNS prefetch disable for privacy
                options.add_argument('--dns-prefetch-disable')
                logger.info("Privacy protection active for direct profile: WebRTC policy + DNS prefetch disabled.")
            if diag_flags.get("no_screen_args"):
                logger.warning(
                    "DIAG: Chrome window-size/window-position arguments skipped. "
                    "Browser will use native/default window behavior."
                )
            else:
                options.add_argument(f'--window-size={launch_window["width"]},{launch_window["height"]}')
                options.add_argument(f'--window-position={launch_window["x"]},{launch_window["y"]}')
            logger.info(
                "Browser launch window prepared: "
                f"{launch_window['width']}x{launch_window['height']}+"
                f"{launch_window['x']},{launch_window['y']} | "
                f"fingerprint_screen={width}x{height} | "
                f"diag_no_screen_args={diag_flags.get('no_screen_args')}"
            )
            
            ua = (
                fingerprint.get('userAgent')
                or fingerprint.get('user_agent')
                or 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
            )
            ua = str(ua or "").strip()
            language_list = self._resolve_language_list(fingerprint)

            language_list = self._jubra_runtime_locale_sync_for_launch(
                fingerprint=fingerprint,
                fallback_languages=language_list,
                proxy_info=proxy_info,
                browser_timezone_session=browser_timezone_session,
                profile_id=profile.get("id"),
            )

            accept_language = self._build_accept_language_header(language_list)
            primary_language = language_list[0] if language_list else "en-US"
            platform_value = self._resolve_navigator_platform(fingerprint, ua)

            # Windows green baseline restore:
            # Keep the fingerprint alignment layers active by default.
            # The earlier native-surface bypass caused PixelScan to see mixed surfaces
            # (native Chromium UA + proxy timezone/profile fingerprint), which triggered
            # Chrome version, timezone and masking warnings. Diagnostic flags below can
            # still disable layers one-by-one when explicitly requested.
            native_surface_mode = False

            self._prepare_chrome_profile_baseline_preferences(
                profile_dir=profile_dir,
                languages=language_list,
                proxy_enabled=bool(proxy_info),
            )
            self._bootstrap_chrome_web_data_for_search_provider(profile_dir)
            for search_arg in self._google_search_provider_launch_arguments():
                options.add_argument(search_arg)
            logger.info("Google omnibox search provider launch arguments prepared.")

            if diag_flags.get("no_ua_arg"):
                logger.warning("DIAG: --user-agent launch argument skipped.")
            else:
                options.add_argument(f'--user-agent={ua}')

            if diag_flags.get("no_lang_arg"):
                logger.warning("DIAG: --lang launch argument skipped.")
            else:
                options.add_argument(f'--lang={primary_language}')
            
            if proxy_info:
                proxy_type = str(proxy_info.get("type", "")).lower()
                
                if proxy_auth_required and proxy_type == "socks5":
                    local_host, local_port = self._start_socks5_auth_bridge(proxy_info)
                    self._wait_for_local_proxy(local_host, local_port)

                    selenium_proxy = Proxy()
                    selenium_proxy.proxy_type = ProxyType.MANUAL
                    selenium_proxy.http_proxy = f"{local_host}:{local_port}"
                    selenium_proxy.ssl_proxy = f"{local_host}:{local_port}"
                    options.proxy = selenium_proxy

                    options.add_argument(f'--proxy-server=http={local_host}:{local_port};https={local_host}:{local_port}')

                    logger.info(
                        f"SOCKS5 auth bridge started via Selenium proxy: "
                        f"http://{local_host}:{local_port} -> {proxy_info['host']}:{proxy_info['port']}"
                    )
                elif proxy_auth_required:
                    proxy_extension_dir = os.path.abspath(os.path.join(profile_dir, "proxy_auth_extension"))
                    self._create_proxy_auth_extension(proxy_extension_dir, proxy_info)
                    options.add_argument(f'--disable-extensions-except={proxy_extension_dir}')
                    options.add_argument(f'--load-extension={proxy_extension_dir}')
                    logger.info(f"Proxy auth extension loaded for {proxy_info['host']}:{proxy_info['port']}")
                else:
                    proxy_str = self._format_proxy_for_chrome(proxy_info)
                    if proxy_str:
                        options.add_argument(f'--proxy-server={proxy_str}')
                        
                        if proxy_type.startswith("socks"):
                            options.add_argument(
                                f'--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE {proxy_info["host"]}'
                            )
            
            if headless:
                options.add_argument('--headless=new')
            
            if self._diag_enabled("JUBRA_DISABLE_ATTACH_MODE"):
                logger.warning(
                    "Remote debug attach mode disabled by JUBRA_DISABLE_ATTACH_MODE=1; "
                    "falling back to ChromeDriver direct launch."
                )
                options.add_argument('--remote-debugging-port=0')
                options.add_argument('--remote-allow-origins=*')

                service = Service(executable_path=self.driver_path)
                driver = webdriver.Chrome(service=service, options=options)
            else:
                driver = self._launch_chrome_subprocess_and_attach(options)

            if diag_flags.get("no_screen_args"):
                logger.warning("DIAG: Selenium window rect set skipped.")
            else:
                try:
                    driver.set_window_rect(
                        x=int(launch_window["x"]),
                        y=int(launch_window["y"]),
                        width=int(launch_window["width"]),
                        height=int(launch_window["height"]),
                    )
                except Exception as e:
                    logger.warning(f"Browser window rect set skipped: {e}")

            # Browser-only timezone mode does not change Windows timezone,
            # so no Windows timezone restore watcher is needed.

            runtime_chrome_version = self._detect_runtime_chrome_version_from_driver(driver)
            if runtime_chrome_version:
                _write_runtime_chrome_version_cache(
                    runtime_chrome_version,
                    source="launcher_cdp",
                    executable_path=self.chromium_path,
                )
                if self._should_runtime_correct_saved_ua(ua, runtime_chrome_version):
                    synced_ua = self._sync_chrome_version_in_user_agent(ua, runtime_chrome_version)
                    if synced_ua and synced_ua != ua:
                        ua = synced_ua
                        fingerprint["userAgent"] = synced_ua
                        fingerprint["user_agent"] = synced_ua
                        self._persist_runtime_synced_user_agent(profile.get("id"), synced_ua)
                        logger.info(
                            "Runtime Chrome version source-of-truth applied to fallback UA: "
                            f"{runtime_chrome_version}"
                        )
            else:
                logger.warning(
                    "Runtime Chrome/Chromium version could not be read from launched browser; "
                    "UA version cache was not updated."
                )
            
            # Cookie/storage policy:
            # Persistent mode preserves the profile's browser state after the first fresh launch.
            # Fresh mode keeps the old disposable-session behavior for future UI/settings toggles.
            if storage_mode == "fresh":
                try:
                    driver.delete_all_cookies()
                    driver.execute_script("localStorage.clear();")
                    driver.execute_script("sessionStorage.clear();")
                    logger.info("Fresh storage mode active; cookies/localStorage/sessionStorage cleared.")
                except Exception as e:
                    logger.warning(f"Fresh storage clear skipped: {e}")
            else:
                logger.info("Persistent storage mode active; cookies and site storage are preserved.")

            if diag_flags.get("no_cdp_identity"):
                logger.warning("DIAG: Runtime UA/Accept-Language/Client-Hints CDP identity layer skipped.")
            else:
                self._apply_runtime_identity_via_cdp(
                    driver=driver,
                    fingerprint=fingerprint,
                    user_agent=ua,
                    platform_value=platform_value,
                    accept_language=accept_language,
                )
            
        # section

            if diag_flags.get("no_stealth"):
                logger.warning("DIAG: selenium_stealth layer skipped.")
            else:
                stealth(driver,
                    languages=language_list,
                    vendor="Google Inc.",
                    platform=platform_value,
                    webgl_vendor=fingerprint.get('webgl', {}).get('vendor', 'Google Inc.'),
                    renderer=fingerprint.get('webgl', {}).get('renderer', 'ANGLE (NVIDIA, NVIDIA GeForce RTX 4070, Direct3D11 vs_5_0)'),
                    fix_hairline=True,
                )

                # selenium_stealth.user_agent_override() calls Browser.getVersion and
                # re-applies the REAL runtime (Windows) User-Agent WITHOUT Client-Hints
                # metadata. On emulated Linux/macOS/Android/iOS profiles this silently
                # reverts navigator.userAgent and the Sec-CH-UA-Platform client hint back
                # to Windows (observed as platform "Win32" in checkers). Re-apply the
                # canonical emulated identity as the final authoritative CDP override so
                # the selected OS/platform + mobile flag always win.
                if not diag_flags.get("no_cdp_identity"):
                    self._apply_runtime_identity_via_cdp(
                        driver=driver,
                        fingerprint=fingerprint,
                        user_agent=ua,
                        platform_value=platform_value,
                        accept_language=accept_language,
                    )
            
        # section

            if diag_flags.get("no_advanced_cdp"):
                logger.warning("DIAG: Advanced fingerprint CDP layer skipped.")
            else:
                self._apply_advanced_fingerprint_via_cdp(driver, fingerprint)
            
            custom_js = fingerprint.get('custom_js')
            if custom_js:
                if diag_flags.get("no_custom_js"):
                    logger.warning("DIAG: profile custom_js injection skipped.")
                else:
                    self._inject_custom_js(driver, custom_js)
            
            launch_timezone = str(fingerprint.get("timezone", "") or "").strip()
            timezone_source = str(fingerprint.get("timezone_source", "") or "").strip().lower()

            allow_timezone_override = False

            if proxy_info:
                if browser_timezone_session:
                    launch_timezone = str(
                        browser_timezone_session.get("iana_timezone", "")
                        or launch_timezone
                    ).strip()

                if not launch_timezone:
                    raise RuntimeError(
                        "Browser-only timezone could not be resolved for proxy profile."
                    )

                # Browser-only mode:
                # Do not change Windows timezone. Apply CDP timezone to current and new tabs.
                if diag_flags.get("no_timezone_cdp"):
                    logger.warning(
                        "DIAG: proxy timezone CDP override/controller skipped: "
                        f"{launch_timezone}"
                    )
                else:
                    allow_timezone_override = True
                    self._set_timezone_via_cdp(driver, launch_timezone)
                    self._start_cdp_timezone_controller(
                        driver=driver,
                        timezone_id=launch_timezone,
                        duration_seconds=7200,
                    )

                    logger.info(
                        f"Browser-only CDP timezone mode active for proxy profile: "
                        f"{launch_timezone}"
                    )

            elif (
                launch_timezone
                and launch_timezone.lower() != "auto"
                and timezone_source == "manual"
            ):
                # No-proxy manual timezone override is allowed only when user selected it.
                if diag_flags.get("no_timezone_cdp"):
                    logger.warning(
                        "DIAG: manual no-proxy timezone CDP override skipped: "
                        f"{launch_timezone}"
                    )
                else:
                    allow_timezone_override = True
                    self._set_timezone_via_cdp(driver, launch_timezone)

            elif launch_timezone and launch_timezone.lower() != "auto":
                logger.info(
                    "No-proxy timezone override skipped because timezone_source is not manual: "
                    f"timezone={launch_timezone} | source={timezone_source or 'missing'}"
                )
            
            if diag_flags.get("no_extra_js"):
                logger.warning("DIAG: extra stealth JavaScript layer skipped.")
            else:
                extra_stealth_script = self._get_extra_stealth_script(
                    fingerprint,
                    allow_timezone_override=allow_timezone_override,
                    language_list=language_list,
                )

                # Apply non-timezone stealth script before every future website script runs.
                # Timezone is handled by browser-only CDP timezone mode.
                self._inject_custom_js(driver, extra_stealth_script)

                # Also apply once to the current active document.
                try:
                    driver.execute_script(extra_stealth_script)
                except Exception as e:
                    logger.warning(f"Immediate extra stealth script execution skipped: {e}")

            if self._diag_enabled("JUBRA_ENABLE_OMNIBOX_FALLBACK"):
                self._start_omnibox_search_fallback_controller(driver)
            else:
                logger.info("Omnibox search fallback controller disabled; using native Web Data search provider.")
            logger.success("Browser launched successfully | Real verification pending")
            return driver, None, None
        except Exception as e:
            # Browser-only timezone mode does not change Windows timezone,
            # so there is nothing to restore here.
            logger.opt(exception=True).critical("Browser launch failed: {}", str(e))
            raise
    
    def _detect_runtime_chrome_version_from_driver(self, driver) -> Optional[str]:
        """Read the real launched browser version from Selenium/CDP source-of-truth."""
        # CDP Browser.getVersion is the most direct runtime source.
        try:
            data = driver.execute_cdp_cmd("Browser.getVersion", {}) or {}
            version = _normalize_runtime_chrome_version(
                data.get("product")
                or data.get("userAgent")
                or ""
            )
            if version:
                logger.info(
                    "Runtime Chrome/Chromium version detected from CDP: "
                    f"{version}"
                )
                return version
        except Exception as exc:
            logger.debug(f"CDP Browser.getVersion probe skipped: {exc}")

        # Selenium capabilities are a safe fallback after the browser is actually running.
        try:
            capabilities = getattr(driver, "capabilities", {}) or {}
            version = _normalize_runtime_chrome_version(
                capabilities.get("browserVersion")
                or capabilities.get("version")
                or ""
            )
            if version:
                logger.info(
                    "Runtime Chrome/Chromium version detected from capabilities: "
                    f"{version}"
                )
                return version
        except Exception as exc:
            logger.debug(f"Selenium capability version probe skipped: {exc}")

        return None

    def _sync_chrome_version_in_user_agent(self, user_agent: str, runtime_version: str) -> str:
        """Replace only the Chrome/x.y.z.w token, preserving OS and UA structure."""
        runtime_version = _normalize_runtime_chrome_version(runtime_version)
        user_agent = str(user_agent or "")
        if not runtime_version or "Chrome/" not in user_agent:
            return user_agent
        return re.sub(r"Chrome/\d+\.\d+\.\d+\.\d+", f"Chrome/{runtime_version}", user_agent, count=1)

    def _should_runtime_correct_saved_ua(self, user_agent: str, runtime_version: str) -> bool:
        """Only auto-correct the known conservative fallback, not user-custom UAs."""
        runtime_version = _normalize_runtime_chrome_version(runtime_version)
        if not runtime_version:
            return False
        ua = str(user_agent or "")
        if "Chrome/120.0.0.0" not in ua:
            return False
        return "Chrome/120.0.0.0" != f"Chrome/{runtime_version}"

    def _persist_runtime_synced_user_agent(self, profile_id, synced_user_agent: str) -> bool:
        """Persist UA version correction for the current profile only when fallback was used."""
        if not profile_id or not synced_user_agent:
            return False

        session = get_session()
        try:
            db_profile = session.query(Profile).filter_by(id=profile_id).first()
            if not db_profile:
                return False

            fingerprint = db_profile.fingerprint or {}
            if not isinstance(fingerprint, dict):
                return False

            existing = str(
                fingerprint.get("userAgent")
                or fingerprint.get("user_agent")
                or ""
            )
            if "Chrome/120.0.0.0" not in existing:
                return False

            updated = json.loads(json.dumps(fingerprint))
            updated["userAgent"] = synced_user_agent
            updated["user_agent"] = synced_user_agent
            db_profile.fingerprint = updated
            session.commit()
            logger.info(
                "Stored profile UA updated from conservative fallback to runtime Chrome version "
                f"for profile_id={profile_id}"
            )
            return True
        except Exception as exc:
            try:
                session.rollback()
            except Exception:
                pass
            logger.warning(f"Runtime-synced UA persistence skipped: {exc}")
            return False
        finally:
            session.close()

    def _resolve_profile_storage_policy(self, fingerprint: Dict) -> Dict[str, str]:
        """Resolve per-profile storage behavior.

        Default is persistent: a newly created profile opens fresh once because its
        storage directory does not exist yet, then cookies/site storage are kept
        across relaunches. Fresh mode is preserved as a controlled fallback for
        future settings/UI toggles.
        """
        storage = fingerprint.get("storage", {}) if isinstance(fingerprint, dict) else {}
        if not isinstance(storage, dict):
            storage = {}

        mode = str(
            storage.get("mode")
            or (fingerprint or {}).get("storage_mode")
            or "persistent"
        ).strip().lower()

        # Privacy-browser default must be stable per profile: a new profile opens
        # with a new empty directory, then the same profile keeps cookies/history
        # across relaunches. Older builds sometimes saved mode="fresh" or
        # "temporary" in the fingerprint, which caused a new timestamped folder
        # on every launch. Treat those legacy values as persistent unless a future
        # UI explicitly opts into an ephemeral session.
        explicit_ephemeral = bool(
            storage.get("ephemeral_session")
            or (fingerprint or {}).get("ephemeral_session")
        )
        if mode in {"temporary", "incognito", "ephemeral", "fresh"} and not explicit_ephemeral:
            logger.warning(
                "Legacy/fresh storage mode found but no explicit ephemeral session was requested; "
                "using persistent profile storage so cookies/history survive relaunch."
            )
            mode = "persistent"
        elif mode in {"temporary", "incognito", "ephemeral"}:
            mode = "fresh"
        if mode not in {"persistent", "fresh"}:
            mode = "persistent"

        storage_id = str(
            storage.get("storage_id")
            or (fingerprint or {}).get("storage_id")
            or ""
        ).strip()
        storage_id = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in storage_id)[:80]

        if mode == "fresh":
            return {
                "mode": "fresh",
                "cookies": "clear_on_launch",
                "localStorage": "clear_on_launch",
                "sessionStorage": "clear_on_launch",
                "cache": "temporary",
                "storage_id": storage_id,
            }

        return {
            "mode": "persistent",
            "cookies": "preserve",
            "localStorage": "preserve",
            "sessionStorage": "browser_default",
            "cache": "preserve",
            "storage_id": storage_id,
        }

    def _safe_profile_storage_dir_name(self, profile_id) -> str:
        safe_id = str(profile_id or "default").strip() or "default"
        safe_id = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in safe_id)
        return f"profile_{safe_id}"

    def _resolve_browser_launch_window(self, fingerprint: Dict) -> Dict[str, int]:
        """Return a polished visual browser window rect for profile launch.

        Fingerprint screen resolution is preserved separately. This method only
        controls the real desktop browser window so first launch and
        minimize/restore behavior feel stable and professional.
        """
        def safe_int(value, default):
            try:
                value = int(value)
                return value if value > 0 else int(default)
            except Exception:
                return int(default)

        work_area = self._get_primary_monitor_work_area()
        work_x = safe_int(work_area.get("x", 0), 0)
        work_y = safe_int(work_area.get("y", 0), 0)
        work_width = safe_int(work_area.get("width", 1440), 1440)
        work_height = safe_int(work_area.get("height", 900), 900)

        horizontal_margin = 80 if work_width >= 1200 else 40
        vertical_margin = 80 if work_height >= 760 else 40

        max_width = max(640, work_width - horizontal_margin)
        max_height = max(520, work_height - vertical_margin)

        # Mobile (Android/iOS) profiles should launch in a narrow, phone-shaped window
        # so the emulated device metrics render like real mobile Chrome instead of a
        # wide desktop window. Desktop profiles keep the polished large window below.
        mobile_family = self._resolve_fingerprint_os_family(fingerprint)
        is_mobile_profile = (
            mobile_family in {"android", "ios"}
            or bool(fingerprint.get("mobile") or fingerprint.get("is_mobile"))
            or str(fingerprint.get("device_type", "")).strip().lower() == "mobile"
        )
        if is_mobile_profile:
            screen = fingerprint.get("screen", {}) if isinstance(fingerprint.get("screen", {}), dict) else {}
            screen_w = safe_int(screen.get("width"), 0)
            screen_h = safe_int(screen.get("height"), 0)
            if not screen_w or not screen_h:
                resolution = str(
                    fingerprint.get("resolution")
                    or fingerprint.get("screen_resolution")
                    or ""
                ).lower().replace(" ", "")
                if "x" in resolution:
                    parts = resolution.split("x", 1)
                    screen_w = safe_int(parts[0], 0) or screen_w
                    screen_h = safe_int(parts[1], 0) or screen_h
            if not screen_w:
                screen_w = 412
            if not screen_h:
                screen_h = 915
            # Small allowance for the browser toolbar/frame around the mobile viewport.
            mobile_width = min(max(screen_w + 16, 360), max_width)
            mobile_height = min(max(screen_h + 120, 640), max_height)
            mobile_x = work_x + max(0, int((work_width - mobile_width) / 2))
            mobile_y = work_y + max(0, int((work_height - mobile_height) / 2))
            return {
                "x": int(mobile_x),
                "y": int(mobile_y),
                "width": int(mobile_width),
                "height": int(mobile_height),
            }

        preferred_width = int(work_width * 0.82)
        preferred_height = int(work_height * 0.86)

        min_width = min(1180, max_width)
        min_height = min(720, max_height)

        target_width = min(max(preferred_width, min_width), max_width)
        target_height = min(max(preferred_height, min_height), max_height)

        x = work_x + max(0, int((work_width - target_width) / 2))
        y = work_y + max(0, int((work_height - target_height) / 2))

        return {
            "x": int(x),
            "y": int(y),
            "width": int(target_width),
            "height": int(target_height),
        }

    def _get_primary_monitor_work_area(self) -> Dict[str, int]:
        """Return the usable desktop work area, excluding taskbar when possible."""
        if os.name != "nt":
            return {"x": 0, "y": 0, "width": 1440, "height": 900}

        try:
            import ctypes

            class RECT(ctypes.Structure):
                _fields_ = [
                    ("left", ctypes.c_long),
                    ("top", ctypes.c_long),
                    ("right", ctypes.c_long),
                    ("bottom", ctypes.c_long),
                ]

            rect = RECT()
            spi_get_work_area = 0x0030
            success = ctypes.windll.user32.SystemParametersInfoW(
                spi_get_work_area,
                0,
                ctypes.byref(rect),
                0,
            )

            if success:
                width = int(rect.right - rect.left)
                height = int(rect.bottom - rect.top)
                if width > 0 and height > 0:
                    return {
                        "x": int(rect.left),
                        "y": int(rect.top),
                        "width": width,
                        "height": height,
                    }

        except Exception as e:
            logger.warning(f"Primary monitor work area detection skipped: {e}")

        return {"x": 0, "y": 0, "width": 1440, "height": 900}

    def _resolve_language_list(self, fingerprint: Dict) -> list:
        """Return a normalized navigator.languages list from the saved profile."""
        raw_languages = fingerprint.get("languages")
        language = str(fingerprint.get("language", "") or "").strip()

        values = []
        if isinstance(raw_languages, (list, tuple)):
            values.extend([str(item).strip() for item in raw_languages if str(item).strip()])
        elif isinstance(raw_languages, str) and raw_languages.strip():
            values.extend([item.strip() for item in raw_languages.split(",") if item.strip()])

        if language:
            values.insert(0, language)

        if not values:
            values = ["en-US", "en"]

        normalized = []
        for item in values:
            if item and item not in normalized:
                normalized.append(item)

        primary = normalized[0]
        short_primary = primary.split("-", 1)[0] if "-" in primary else ""
        if short_primary and short_primary not in normalized:
            normalized.append(short_primary)

        return normalized[:4]
    def _jubra_locale_chain_for_country(self, country_code: str, fallback_languages: list = None) -> list:
        country = str(country_code or "").strip().upper()

        locale_map = {
            "BD": ["en-BD", "en", "bn-BD", "bn"],
            "US": ["en-US", "en"],
            "GB": ["en-GB", "en"],
            "CA": ["en-CA", "en", "fr-CA", "fr"],
            "AU": ["en-AU", "en"],
            "DE": ["de-DE", "de", "en"],
            "FR": ["fr-FR", "fr", "en"],
            "IT": ["it-IT", "it", "en"],
            "ES": ["es-ES", "es", "en"],
            "NL": ["nl-NL", "nl", "en"],
            "IN": ["en-IN", "en", "hi-IN", "hi"],
            "PK": ["en-PK", "en", "ur-PK", "ur"],
            "AE": ["en-AE", "en", "ar-AE", "ar"],
            "SA": ["ar-SA", "ar", "en"],
            "SG": ["en-SG", "en"],
            "MY": ["ms-MY", "ms", "en"],
            "JP": ["ja-JP", "ja", "en"],
            "KR": ["ko-KR", "ko", "en"],
            "CN": ["zh-CN", "zh", "en"],
        }

        resolved = locale_map.get(country)
        if resolved:
            return resolved[:4]

        fallback = [str(item).strip() for item in (fallback_languages or []) if str(item).strip()]
        return fallback[:4] if fallback else ["en-US", "en"]

    def _jubra_country_from_timezone_hint(self, timezone_value: str) -> str:
        text = str(timezone_value or "").strip().lower()

        timezone_country_map = {
            "asia/dhaka": "BD",
            "bangladesh standard time": "BD",
            "gmt+0600": "BD",
            "utc+0600": "BD",
            "+0600": "BD",

            "america/new_york": "US",
            "america/chicago": "US",
            "america/denver": "US",
            "america/los_angeles": "US",
            "eastern standard time": "US",
            "central standard time": "US",
            "mountain standard time": "US",
            "pacific standard time": "US",

            "europe/london": "GB",
            "gmt standard time": "GB",
            "europe/berlin": "DE",
            "w. europe standard time": "DE",
            "europe/paris": "FR",
            "romance standard time": "FR",
            "asia/dubai": "AE",
            "arabian standard time": "AE",
            "asia/kolkata": "IN",
            "india standard time": "IN",
        }

        return timezone_country_map.get(text, "")

    def _jubra_collect_direct_geo_for_locale(self, timeout: float = 6.0) -> Dict:
        endpoints = [
            "https://ipwho.is/",
            "https://ipapi.co/json/",
            "http://ip-api.com/json/?fields=status,country,countryCode,regionName,city,timezone,query",
        ]

        for endpoint in endpoints:
            try:
                response = requests.get(endpoint, timeout=timeout)
                response.raise_for_status()
                data = response.json()

                geo = self._extract_geo_timezone_from_data(data)
                country_code = str(geo.get("country_code", "") or "").strip().upper()
                timezone_value = str(geo.get("timezone", "") or "").strip()

                if country_code:
                    logger.info(
                        "Direct runtime locale geo resolved: "
                        f"ip={geo.get('ip', '')} | country={country_code} | timezone={timezone_value}"
                    )
                    return geo

            except Exception as exc:
                logger.warning(f"Direct runtime locale geo endpoint skipped: {endpoint} | {exc}")

        return {}

    def _jubra_persist_runtime_locale(
        self,
        profile_id,
        languages: list,
        source: str,
        country_code: str = "",
        timezone_value: str = "",
    ) -> bool:
        if not profile_id or not languages:
            return False

        session = get_session()
        try:
            db_profile = session.query(Profile).filter_by(id=profile_id).first()
            if not db_profile:
                return False

            fingerprint = db_profile.fingerprint or {}
            if not isinstance(fingerprint, dict):
                return False

            updated = json.loads(json.dumps(fingerprint))
            updated["language"] = str(languages[0])
            updated["languages"] = [str(item) for item in languages]
            updated["language_source"] = "runtime_ip_geo"
            updated["locale_source"] = str(source or "runtime")
            updated["locale_country_code"] = str(country_code or "").upper()

            if timezone_value:
                updated["runtime_geo_timezone"] = str(timezone_value)

            canonical = dict(updated)
            canonical.pop("fingerprint_hash", None)
            synced_hash = hashlib.sha256(
                json.dumps(canonical, sort_keys=True).encode()
            ).hexdigest()
            canonical["fingerprint_hash"] = synced_hash

            db_profile.fingerprint = canonical
            db_profile.fingerprint_hash = synced_hash
            session.commit()

            logger.info(
                "Stored profile locale synced from runtime locale resolver: "
                f"profile_id={profile_id} | source={source} | "
                f"country={country_code or 'unknown'} | languages={','.join(languages)}"
            )
            return True

        except Exception as exc:
            try:
                session.rollback()
            except Exception:
                pass
            logger.warning(f"Runtime locale persistence skipped: {exc}")
            return False
        finally:
            session.close()

    def _jubra_runtime_locale_sync_for_launch(
        self,
        fingerprint: Dict,
        fallback_languages: list,
        proxy_info: Dict = None,
        browser_timezone_session: Dict = None,
        profile_id=None,
    ) -> list:
        geo = {}
        source = "profile"

        if proxy_info and isinstance(browser_timezone_session, dict):
            geo = browser_timezone_session.get("proxy_geo", {}) or {}
            source = "proxy"

        elif not proxy_info:
            geo = self._jubra_collect_direct_geo_for_locale()
            source = "direct" if geo else "direct_timezone_fallback"

        country_code = str(geo.get("country_code", "") or "").strip().upper()
        timezone_value = str(geo.get("timezone", "") or "").strip()

        if not country_code and not proxy_info:
            timezone_hint = ""

            try:
                timezone_hint = str(self._get_current_windows_timezone() or "")
            except Exception:
                timezone_hint = ""

            if not timezone_hint:
                timezone_hint = str(
                    fingerprint.get("runtime_geo_timezone")
                    or fingerprint.get("timezone")
                    or ""
                )

            country_code = self._jubra_country_from_timezone_hint(timezone_hint)
            timezone_value = timezone_value or timezone_hint

            if country_code:
                logger.info(
                    "Direct runtime locale resolved from system timezone fallback: "
                    f"timezone={timezone_hint} | country={country_code}"
                )

        languages = self._jubra_locale_chain_for_country(country_code, fallback_languages)

        if isinstance(fingerprint, dict):
            fingerprint["language"] = languages[0]
            fingerprint["languages"] = languages
            fingerprint["language_source"] = "runtime_ip_geo"
            fingerprint["locale_source"] = source
            fingerprint["locale_country_code"] = country_code
            if timezone_value:
                fingerprint["runtime_geo_timezone"] = timezone_value

        if country_code:
            logger.info(
                "Runtime locale sync applied: "
                f"source={source} | country={country_code} | languages={','.join(languages)}"
            )
            self._jubra_persist_runtime_locale(
                profile_id=profile_id,
                languages=languages,
                source=source,
                country_code=country_code,
                timezone_value=timezone_value,
            )
        else:
            logger.warning(
                "Runtime locale sync used fallback languages because country was unavailable: "
                f"{','.join(languages)}"
            )

        return languages[:4]

    def _build_accept_language_header(self, languages: list) -> str:
        """Build a realistic Accept-Language header from navigator.languages."""
        normalized = [str(item).strip() for item in languages if str(item).strip()]
        if not normalized:
            normalized = ["en-US", "en"]

        weighted = []
        for index, language in enumerate(normalized[:4]):
            if index == 0:
                weighted.append(language)
            else:
                quality = max(0.5, 1.0 - (index * 0.1))
                weighted.append(f"{language};q={quality:.1f}")

        return ",".join(weighted)

    def _resolve_navigator_platform(self, fingerprint: Dict, user_agent: str = "") -> str:
        """Resolve navigator.platform from profile OS/UA to avoid cross-OS leaks."""
        os_value = str(
            fingerprint.get("os")
            or fingerprint.get("os_type")
            or fingerprint.get("platform_os")
            or ""
        ).strip().lower()
        ua = str(user_agent or fingerprint.get("userAgent") or fingerprint.get("user_agent") or "")

        # Mobile families first: Android UAs contain "Linux" and iOS UAs contain "Mac OS X".
        if "android" in os_value or "Android" in ua:
            return "Linux armv8l"
        if any(token in os_value for token in ("ios", "iphone", "ipad")) or "iPhone" in ua or "iPad" in ua:
            return "iPhone"
        if "mac" in os_value or "Macintosh" in ua or "Mac OS X" in ua:
            return "MacIntel"
        if "linux" in os_value or "Linux" in ua or "X11" in ua:
            return "Linux x86_64"
        if "win" in os_value or "Windows" in ua:
            return "Win32"

        platform = str(fingerprint.get("platform", "") or "").strip()
        return platform or "Win32"

    def _extract_chrome_version_parts(self, user_agent: str) -> Tuple[str, str]:
        ua = str(user_agent or "")
        version = "120.0.0.0"
        for marker in ("Chrome/", "CriOS/"):
            if marker in ua:
                version = ua.split(marker, 1)[1].split(" ", 1)[0].strip() or version
                break
        major = version.split(".", 1)[0] if version else "120"
        if not major.isdigit():
            major = "120"
        return major, version

    def _client_hints_platform_from_ua(self, user_agent: str, platform_value: str) -> str:
        ua = str(user_agent or "")
        platform = str(platform_value or "")
        # Mobile families first: Android UAs contain "Linux" and iOS UAs contain "Mac".
        if "Android" in ua:
            return "Android"
        if "iPhone" in ua or "iPad" in ua or platform == "iPhone":
            return "iOS"
        if "Macintosh" in ua or platform.startswith("Mac"):
            return "macOS"
        if "Linux" in ua or platform.startswith("Linux"):
            return "Linux"
        return "Windows"

    def _build_user_agent_metadata(self, user_agent: str, platform_value: str) -> Dict:
        """Build Chromium Client Hints metadata matching the saved UA/platform."""
        major, full_version = self._extract_chrome_version_parts(user_agent)
        ch_platform = self._client_hints_platform_from_ua(user_agent, platform_value)
        architecture = "x86"
        bitness = "64"
        platform_version = "10.0.0"
        mobile = False
        model = ""

        if ch_platform == "macOS":
            platform_version = "14.0.0"
        elif ch_platform == "Linux":
            platform_version = "6.0.0"
        elif ch_platform == "Android":
            # Mobile Client Hints do not expose desktop CPU architecture/bitness.
            # Derive the Android version and device model from the UA so the
            # Sec-CH-UA-Platform-Version and Sec-CH-UA-Model headers always match
            # the (per-profile varied) user-agent string. Falls back to sane
            # defaults if the UA cannot be parsed.
            architecture = ""
            bitness = ""
            mobile = True
            ver_match = re.search(r"Android\s+(\d+)", user_agent)
            android_major = ver_match.group(1) if ver_match else "13"
            platform_version = f"{android_major}.0.0"
            model_match = re.search(r"Android\s+\d+(?:\.\d+)*;\s*([^;)]+?)\s*\)", user_agent)
            model = model_match.group(1).strip() if model_match else "Pixel 7"
        elif ch_platform == "iOS":
            # iOS Safari/Chrome does not expose a device model via Client Hints;
            # only derive the iOS version from the UA to keep the header coherent.
            architecture = ""
            bitness = ""
            mobile = True
            model = ""
            ios_match = re.search(r"iPhone OS (\d+)[_.](\d+)", user_agent)
            if ios_match:
                platform_version = f"{ios_match.group(1)}.{ios_match.group(2)}.0"
            else:
                platform_version = "17.0.0"

        return {
            "brands": [
                {"brand": "Chromium", "version": major},
                {"brand": "Google Chrome", "version": major},
                {"brand": "Not A(Brand", "version": "24"},
            ],
            "fullVersionList": [
                {"brand": "Chromium", "version": full_version},
                {"brand": "Google Chrome", "version": full_version},
                {"brand": "Not A(Brand", "version": "24.0.0.0"},
            ],
            "fullVersion": full_version,
            "platform": ch_platform,
            "platformVersion": platform_version,
            "architecture": architecture,
            "bitness": bitness,
            "model": model,
            "mobile": mobile,
            "wow64": False,
        }

    def _apply_runtime_identity_via_cdp(
        self,
        driver,
        fingerprint: Dict,
        user_agent: str,
        platform_value: str,
        accept_language: str,
    ):
        """Apply HTTP UA, Accept-Language and Client Hints before real navigation."""
        try:
            metadata = self._build_user_agent_metadata(user_agent, platform_value)
            payload = {
                "userAgent": str(user_agent),
                "acceptLanguage": str(accept_language or "en-US,en;q=0.9"),
                "platform": str(platform_value or "Win32"),
                "userAgentMetadata": metadata,
            }

            try:
                driver.execute_cdp_cmd("Network.enable", {})
            except Exception:
                pass

            try:
                driver.execute_cdp_cmd("Network.setUserAgentOverride", payload)
            except Exception as metadata_error:
                logger.warning(
                    f"Client Hints metadata override skipped, retrying basic UA override: {metadata_error}"
                )
                payload.pop("userAgentMetadata", None)
                driver.execute_cdp_cmd("Network.setUserAgentOverride", payload)

            logger.info(
                "Runtime browser identity applied via CDP: "
                f"platform={platform_value} | acceptLanguage={accept_language}"
            )

        except Exception as e:
            logger.warning(f"Runtime browser identity CDP apply skipped: {e}")

    def _apply_advanced_fingerprint_via_cdp(self, driver, fingerprint: Dict):
        try:
            try:
                driver.execute_cdp_cmd('Emulation.setColorDepth', {'depth': fingerprint.get('color_depth', 24)})
            except:
                pass
            try:
                is_mobile_profile = bool(
                    fingerprint.get("mobile")
                    or fingerprint.get("is_mobile")
                    or str(fingerprint.get("device_type", "")).lower() == "mobile"
                    or self._resolve_fingerprint_os_family(fingerprint) in {"android", "ios"}
                )

                if is_mobile_profile:
                    driver.execute_cdp_cmd('Emulation.setDeviceMetricsOverride', {
                        'width': fingerprint.get('screen', {}).get('width', 390),
                        'height': fingerprint.get('screen', {}).get('height', 844),
                        'deviceScaleFactor': fingerprint.get('pixel_ratio', 2.0),
                        'mobile': True
                    })

                    # Canonical mobile emulation also needs the Emulation-domain UA
                    # override (with mobile Client-Hints metadata). Network.setUserAgentOverride
                    # alone leaves some contexts reading the desktop identity, which made
                    # Android/iOS profiles surface as "Windows" on checkers. Applied only
                    # for mobile profiles, so desktop (incl. Windows) behavior is untouched.
                    try:
                        mobile_ua = str(
                            fingerprint.get("userAgent")
                            or fingerprint.get("user_agent")
                            or ""
                        ).strip()
                        if mobile_ua:
                            mobile_platform = self._resolve_navigator_platform(fingerprint, mobile_ua)
                            mobile_ua_override = {
                                "userAgent": mobile_ua,
                                "platform": mobile_platform,
                                "userAgentMetadata": self._build_user_agent_metadata(mobile_ua, mobile_platform),
                            }
                            try:
                                driver.execute_cdp_cmd("Emulation.setUserAgentOverride", mobile_ua_override)
                            except Exception:
                                mobile_ua_override.pop("userAgentMetadata", None)
                                driver.execute_cdp_cmd("Emulation.setUserAgentOverride", mobile_ua_override)
                            logger.info(
                                "Mobile emulation UA override applied via Emulation domain: "
                                f"platform={mobile_platform}"
                            )
                    except Exception as mobile_ua_error:
                        logger.warning(f"Mobile Emulation UA override skipped: {mobile_ua_error}")
                else:
                    desktop_screen = fingerprint.get('screen', {}) if isinstance(fingerprint.get('screen', {}), dict) else {}
                    desktop_width = int(desktop_screen.get('width') or 1920)
                    desktop_height = int(desktop_screen.get('height') or 1080)
                    desktop_scale = float(
                        desktop_screen.get('devicePixelRatio')
                        or fingerprint.get('pixel_ratio')
                        or 1.0
                    )

                    driver.execute_cdp_cmd('Emulation.setDeviceMetricsOverride', {
                        'width': desktop_width,
                        'height': desktop_height,
                        'deviceScaleFactor': desktop_scale,
                        'mobile': False
                    })

                    desktop_ua = str(
                        fingerprint.get("userAgent")
                        or fingerprint.get("user_agent")
                        or ""
                    ).strip()
                    if desktop_ua:
                        desktop_platform = self._resolve_navigator_platform(fingerprint, desktop_ua)
                        desktop_accept_language = self._build_accept_language_header(
                            self._resolve_language_list(fingerprint)
                        )
                        desktop_ua_override = {
                            "userAgent": desktop_ua,
                            "acceptLanguage": desktop_accept_language,
                            "platform": desktop_platform,
                            "userAgentMetadata": self._build_user_agent_metadata(desktop_ua, desktop_platform),
                        }
                        try:
                            driver.execute_cdp_cmd("Emulation.setUserAgentOverride", desktop_ua_override)
                        except Exception:
                            desktop_ua_override.pop("userAgentMetadata", None)
                            driver.execute_cdp_cmd("Emulation.setUserAgentOverride", desktop_ua_override)
                        logger.info(
                            "Desktop Emulation UA override applied: "
                            f"platform={desktop_platform}"
                        )

                    logger.info(
                        f"Desktop device metrics override applied: "
                        f"{desktop_width}x{desktop_height} @ DPR {desktop_scale}"
                    )
            except Exception as e:
                logger.warning(f"Device metrics override skipped: {e}")
            try:
                driver.execute_cdp_cmd('Emulation.setTouchEmulationEnabled', {
                    'enabled': fingerprint.get('touch_points', 0) > 0,
                    'maxTouchPoints': fingerprint.get('touch_points', 0)
                })
            except:
                pass
            logger.info("Advanced fingerprint applied via CDP (partial success allowed)")
        except Exception as e:
            logger.warning(f"CDP fingerprint application partially failed: {e}")
    
    def _set_timezone_via_cdp(self, driver, timezone: str):
        try:
            driver.execute_cdp_cmd('Emulation.setTimezoneOverride', {'timezoneId': timezone})
            logger.info(f"Timezone set to: {timezone}")
        except Exception as e:
            logger.warning(f"Failed to set timezone: {e}")
            
    def _resolve_stored_proxy_timezone_fallback(self, proxy_info: Dict, fingerprint: Dict) -> str:
        """Find a stored proxy timezone when live proxy geo preflight is unavailable."""
        candidates = []
        if isinstance(fingerprint, dict):
            candidates.extend([
                fingerprint.get("runtime_geo_timezone"),
                fingerprint.get("proxy_timezone"),
                fingerprint.get("timezone"),
            ])
        if isinstance(proxy_info, dict):
            candidates.extend([
                proxy_info.get("timezone"),
                proxy_info.get("iana_timezone"),
            ])

        for value in candidates:
            timezone_value = str(value or "").strip()
            if timezone_value and timezone_value.lower() not in {"auto", "system", "default", "none", "null"}:
                return timezone_value

        country_code = ""
        if isinstance(fingerprint, dict):
            country_code = str(
                fingerprint.get("locale_country_code")
                or fingerprint.get("country_code")
                or ""
            ).strip().upper()
        if not country_code and isinstance(proxy_info, dict):
            country_code = str(proxy_info.get("country_code") or proxy_info.get("country") or "").strip().upper()

        country_fallbacks = {
            "US": "America/New_York",
            "GB": "Europe/London",
            "UK": "Europe/London",
            "CA": "America/Toronto",
            "AU": "Australia/Sydney",
            "DE": "Europe/Berlin",
            "FR": "Europe/Paris",
            "NL": "Europe/Amsterdam",
        }
        return country_fallbacks.get(country_code, "")

    def _prepare_browser_proxy_timezone(self, proxy_info: Dict, fingerprint: Dict) -> Dict:
        """
        Browser-only timezone mode.

        Resolve proxy timezone before Chrome starts, but do not change Windows timezone.
        The resolved IANA timezone is later applied through CDP to browser tabs.
        """
        try:
            proxy_geo = self._collect_proxy_geo_via_requests(proxy_info)
        except Exception as exc:
            fallback_timezone = self._resolve_stored_proxy_timezone_fallback(proxy_info, fingerprint)
            if not fallback_timezone:
                raise RuntimeError(
                    "Proxy timezone could not be resolved before browser launch and no stored "
                    "proxy timezone fallback was available."
                ) from exc

            proxy_geo = {
                "timezone": fallback_timezone,
                "ip": "",
                "city": "",
                "country_code": str(
                    fingerprint.get("locale_country_code")
                    or proxy_info.get("country_code")
                    or proxy_info.get("country")
                    or ""
                ).upper(),
                "source": "stored_proxy_timezone_fallback",
                "preflight_error": str(exc),
            }
            logger.warning(
                "Proxy geo preflight failed before launch; using stored proxy timezone fallback: "
                f"iana={fallback_timezone}"
            )

        proxy_timezone = str(proxy_geo.get("timezone", "") or "").strip()

        if not proxy_timezone:
            raise RuntimeError(
                "Proxy timezone could not be resolved before browser launch. "
                "Launch blocked to avoid timezone privacy mismatch."
            )

        fingerprint["timezone"] = proxy_timezone

        if "timezone_offset" in fingerprint:
            fingerprint.pop("timezone_offset", None)

        logger.info(
            "Browser-only proxy timezone prepared before browser launch: "
            f"iana={proxy_timezone} | "
            f"ip={proxy_geo.get('ip', '')} | city={proxy_geo.get('city', '')} | "
            f"country={proxy_geo.get('country_code', '')}"
        )

        return {
            "iana_timezone": proxy_timezone,
            "proxy_geo": proxy_geo,
        }            
            
    def _collect_proxy_geo_via_requests(self, proxy_info: Dict, timeout: float = 15.0) -> Dict:
        """
        Resolve proxy public IP/timezone before Chrome launch using Python requests through the proxy.
        This lets us set the OS timezone before Chromium starts.
        """
        proxy_url = self._build_requests_proxy_url(proxy_info)

        if not proxy_url:
            raise RuntimeError("Could not build proxy URL for native timezone resolution.")

        proxies = {
            "http": proxy_url,
            "https": proxy_url,
        }

        expected_proxy_ip = self._get_expected_proxy_ip(proxy_info)

        endpoints = [
            "https://ipwho.is/",
            "https://ipapi.co/json/",
        ]

        last_error = None
        last_geo = {}

        for endpoint in endpoints:
            try:
                response = requests.get(endpoint, proxies=proxies, timeout=timeout)
                response.raise_for_status()
                data = response.json()

                geo = self._extract_geo_timezone_from_data(data)
                geo["endpoint"] = endpoint
                last_geo = geo

                timezone_value = str(geo.get("timezone", "") or "").strip()
                geo_ip_raw = str(geo.get("ip", "") or "").strip()
                geo_ip = self._normalize_ip_value(geo_ip_raw)

                if not timezone_value:
                    last_error = f"No timezone returned from {endpoint}"
                    continue

                if expected_proxy_ip:
                    if geo_ip and geo_ip == expected_proxy_ip:
                        logger.info(
                            f"Verified proxy route before launch: "
                            f"geo_ip={geo_ip} matched expected_proxy_ip={expected_proxy_ip}"
                        )
                        return geo

                    last_error = (
                        "Proxy geo IP did not match expected proxy IP before launch: "
                        f"endpoint={endpoint} | geo_ip={geo_ip_raw or 'none'} | "
                        f"expected_proxy_ip={expected_proxy_ip}"
                    )
                    logger.warning(last_error)
                    continue

                logger.info(
                    "Proxy host is not a direct IP, accepting proxy geo timezone before launch: "
                    f"geo_ip={geo_ip_raw or 'unknown'} | timezone={timezone_value}"
                )
                return geo

            except Exception as e:
                last_error = f"{endpoint} failed: {e}"
                logger.warning(f"Proxy geo request failed before launch: {last_error}")

        last_geo_text = json.dumps(last_geo, ensure_ascii=False, default=str) if last_geo else "none"
        raise RuntimeError(
            "Proxy timezone could not be verified before browser launch. "
            f"Last error: {last_error or 'unknown'} | Last geo: {last_geo_text}"
        )

    def _build_requests_proxy_url(self, proxy_info: Dict) -> Optional[str]:
        try:
            proxy_type = str(proxy_info.get("type", "http") or "http").lower()
            host = str(proxy_info.get("host", "") or "").strip()
            port = proxy_info.get("port")
            username = proxy_info.get("username")
            password = proxy_info.get("password")

            if not host or not port:
                return None

            if proxy_type not in ("http", "https", "socks4", "socks5"):
                proxy_type = "http"

            auth_part = ""
            if username or password:
                auth_part = f"{quote(str(username or ''))}:{quote(str(password or ''))}@"

            return f"{proxy_type}://{auth_part}{host}:{int(port)}"

        except Exception as e:
            logger.warning(f"Failed to build requests proxy URL: {e}")
            return None

    def _extract_geo_timezone_from_data(self, data: Dict) -> Dict:
        if not isinstance(data, dict):
            return {}

        raw_timezone = data.get("timezone")
        timezone_value = ""

        if isinstance(raw_timezone, dict):
            timezone_value = str(
                raw_timezone.get("id")
                or raw_timezone.get("timezone")
                or raw_timezone.get("name")
                or ""
            ).strip()
        else:
            timezone_value = str(raw_timezone or "").strip()

        ip_value = (
            data.get("ip")
            or data.get("query")
            or data.get("address")
            or ""
        )

        return {
            "ip": str(ip_value or "").strip(),
            "timezone": timezone_value,
            "city": str(data.get("city", "") or ""),
            "region": str(data.get("region", "") or data.get("regionName", "") or ""),
            "country": str(data.get("country", "") or data.get("country_name", "") or ""),
            "country_code": str(data.get("country_code", "") or data.get("countryCode", "") or ""),
        }

    def _get_current_windows_timezone(self) -> str:
        result = subprocess.run(
            ["tzutil", "/g"],
            capture_output=True,
            text=True,
            timeout=10,
        )

        if result.returncode != 0:
            raise RuntimeError(
                f"Failed to get Windows timezone: {result.stderr.strip()}"
            )

        return str(result.stdout or "").strip()

    def _activate_native_windows_timezone(
        self,
        session_id: str,
        windows_timezone: str,
        iana_timezone: str,
    ):
        with self._native_tz_lock:
            current_timezone = self._get_current_windows_timezone()

            active_timezones = set(self._native_tz_sessions.values())
            if active_timezones and windows_timezone not in active_timezones:
                raise RuntimeError(
                    "Cannot launch multiple native-timezone browser sessions with different timezones. "
                    f"Active={active_timezones}, requested={windows_timezone}"
                )

            if self._native_tz_original is None:
                self._native_tz_original = current_timezone

            if current_timezone != windows_timezone:
                logger.info(
                    f"Setting Windows timezone for native browser consistency: "
                    f"{current_timezone} -> {windows_timezone} ({iana_timezone})"
                )
                self._set_windows_timezone(windows_timezone)
            else:
                logger.info(
                    f"Windows timezone already matches requested native timezone: {windows_timezone}"
                )

            self._native_tz_sessions[session_id] = windows_timezone

    def _start_native_timezone_restore_watcher(self, driver, session_info: Dict):
        try:
            watcher = threading.Thread(
                target=self._native_timezone_restore_loop,
                args=(driver, session_info),
                daemon=True,
            )
            watcher.start()

            logger.info(
                "Native timezone restore watcher started: "
                f"session={session_info.get('session_id')} | "
                f"windows={session_info.get('windows_timezone')}"
            )

        except Exception as e:
            logger.warning(f"Failed to start native timezone restore watcher: {e}")

    def _native_timezone_restore_loop(self, driver, session_info: Dict):
        session_id = str(session_info.get("session_id", "") or "")

        try:
            service = getattr(driver, "service", None)
            process = getattr(service, "process", None)

            # Do not call driver.window_handles here.
            # Selenium driver access from a background thread can fail while the browser is still open,
            # causing timezone to restore too early.
            while True:
                if process is not None:
                    try:
                        if process.poll() is not None:
                            break
                    except Exception:
                        pass

                time.sleep(2.0)

        finally:
            self._release_native_timezone_session(session_id)

    def _release_native_timezone_session(self, session_id: str):
        with self._native_tz_lock:
            if session_id:
                self._native_tz_sessions.pop(session_id, None)

            if self._native_tz_sessions:
                return

            original_timezone = self._native_tz_original
            self._native_tz_original = None

            if not original_timezone:
                return

            try:
                current_timezone = self._get_current_windows_timezone()

                if current_timezone != original_timezone:
                    logger.info(
                        f"Restoring Windows timezone after browser close: "
                        f"{current_timezone} -> {original_timezone}"
                    )
                    self._set_windows_timezone(original_timezone)
                else:
                    logger.info("Windows timezone already restored.")

            except Exception as e:
                logger.warning(f"Failed to restore original Windows timezone: {e}")                       

    def _iana_to_windows_timezone(self, iana_timezone: str) -> Optional[str]:
        """
        Minimal IANA to Windows timezone mapping.
        Expand this dictionary as more proxy regions are used.
        """
        tz = str(iana_timezone or "").strip()

        if not tz:
            return None

        if tz.endswith(" Standard Time") or tz in ("UTC", "GMT Standard Time"):
            return tz

        mapping = {
            "UTC": "UTC",
            "Etc/UTC": "UTC",
            "Etc/GMT": "UTC",

            "America/New_York": "Eastern Standard Time",
            "America/Detroit": "Eastern Standard Time",
            "America/Toronto": "Eastern Standard Time",
            "America/Montreal": "Eastern Standard Time",
            "America/Nassau": "Eastern Standard Time",

            "America/Chicago": "Central Standard Time",
            "America/Winnipeg": "Central Standard Time",
            "America/Mexico_City": "Central Standard Time",
            "America/Guatemala": "Central America Standard Time",

            "America/Denver": "Mountain Standard Time",
            "America/Edmonton": "Mountain Standard Time",
            "America/Phoenix": "US Mountain Standard Time",

            "America/Los_Angeles": "Pacific Standard Time",
            "America/Vancouver": "Pacific Standard Time",
            "America/Tijuana": "Pacific Standard Time",

            "America/Anchorage": "Alaskan Standard Time",
            "Pacific/Honolulu": "Hawaiian Standard Time",

            "America/Sao_Paulo": "E. South America Standard Time",
            "America/Buenos_Aires": "Argentina Standard Time",
            "America/Bogota": "SA Pacific Standard Time",
            "America/Lima": "SA Pacific Standard Time",
            "America/Santiago": "Pacific SA Standard Time",

            "Europe/London": "GMT Standard Time",
            "Europe/Dublin": "GMT Standard Time",
            "Europe/Lisbon": "GMT Standard Time",

            "Europe/Paris": "Romance Standard Time",
            "Europe/Madrid": "Romance Standard Time",
            "Europe/Rome": "W. Europe Standard Time",
            "Europe/Berlin": "W. Europe Standard Time",
            "Europe/Amsterdam": "W. Europe Standard Time",
            "Europe/Brussels": "Romance Standard Time",
            "Europe/Vienna": "W. Europe Standard Time",
            "Europe/Warsaw": "Central European Standard Time",
            "Europe/Prague": "Central Europe Standard Time",
            "Europe/Zurich": "W. Europe Standard Time",
            "Europe/Stockholm": "W. Europe Standard Time",
            "Europe/Helsinki": "FLE Standard Time",
            "Europe/Athens": "GTB Standard Time",
            "Europe/Istanbul": "Turkey Standard Time",
            "Europe/Moscow": "Russian Standard Time",

            "Africa/Cairo": "Egypt Standard Time",
            "Africa/Johannesburg": "South Africa Standard Time",
            "Africa/Nairobi": "E. Africa Standard Time",

            "Asia/Dhaka": "Bangladesh Standard Time",
            "Asia/Kolkata": "India Standard Time",
            "Asia/Calcutta": "India Standard Time",
            "Asia/Karachi": "Pakistan Standard Time",
            "Asia/Dubai": "Arabian Standard Time",
            "Asia/Riyadh": "Arab Standard Time",
            "Asia/Bangkok": "SE Asia Standard Time",
            "Asia/Jakarta": "SE Asia Standard Time",
            "Asia/Singapore": "Singapore Standard Time",
            "Asia/Kuala_Lumpur": "Singapore Standard Time",
            "Asia/Manila": "Singapore Standard Time",
            "Asia/Hong_Kong": "China Standard Time",
            "Asia/Shanghai": "China Standard Time",
            "Asia/Taipei": "Taipei Standard Time",
            "Asia/Tokyo": "Tokyo Standard Time",
            "Asia/Seoul": "Korea Standard Time",

            "Australia/Sydney": "AUS Eastern Standard Time",
            "Australia/Melbourne": "AUS Eastern Standard Time",
            "Australia/Brisbane": "E. Australia Standard Time",
            "Australia/Perth": "W. Australia Standard Time",
            "Pacific/Auckland": "New Zealand Standard Time",
        }

        return mapping.get(tz)            
            
    def _resolve_launch_timezone(
        self,
        driver,
        fingerprint: Dict,
        proxy_info: Optional[Dict] = None,
    ) -> Optional[str]:
        """
        Resolve the timezone for this browser launch.

        Strict privacy rule:
        - If proxy is configured, timezone must come from a verified browser proxy route.
        - Do not silently fall back to local/profile timezone while proxy is configured.
        """
        profile_timezone = str(fingerprint.get("timezone", "") or "").strip()

        if proxy_info:
            logger.info("Waiting for verified browser proxy route before timezone sync...")

            proxy_timezone, proxy_geo = self._collect_proxy_timezone_from_browser(
                driver=driver,
                proxy_info=proxy_info,
                timeout=25.0,
                retry_delay=1.5,
            )

            if proxy_timezone:
                logger.info(
                    "Proxy timezone resolved from verified browser route: "
                    f"{proxy_timezone} | "
                    f"ip={proxy_geo.get('ip', '')} | "
                    f"city={proxy_geo.get('city', '')} | "
                    f"country={proxy_geo.get('country_code', '')}"
                )
                return proxy_timezone

            raise RuntimeError(
                "Proxy timezone could not be verified from the browser proxy route. "
                "Launch blocked to avoid timezone privacy mismatch."
            )

        if profile_timezone and profile_timezone.lower() != "auto":
            return profile_timezone

        return None

    def _normalize_ip_value(self, value) -> Optional[str]:
        try:
            if value is None:
                return None

            value = str(value).strip().strip("[](){}<>,;\"'")
            return str(ipaddress.ip_address(value))
        except Exception:
            return None

    def _get_expected_proxy_ip(self, proxy_info: Optional[Dict]) -> Optional[str]:
        if not proxy_info:
            return None

        host = str(proxy_info.get("host", "") or "").strip()
        return self._normalize_ip_value(host)

    def _collect_proxy_timezone_from_browser(
        self,
        driver,
        proxy_info: Optional[Dict],
        timeout: float = 25.0,
        retry_delay: float = 1.5,
    ) -> Tuple[Optional[str], Dict]:
        """
        Collect proxy/public IP timezone using browser traffic only.

        If proxy host is a direct IP, geo endpoint IP must match it before
        timezone is accepted. This prevents local timezone fallback being used
        before proxy extension/route is ready.
        """
        original_handle = None
        verification_handle = None
        original_handles = []
        last_geo_data = {}

        expected_proxy_ip = self._get_expected_proxy_ip(proxy_info)
        expected_proxy_host = str((proxy_info or {}).get("host", "") or "").strip()

        geo_endpoints = [
            "https://ipwho.is/",
            "https://ipapi.co/json/",
        ]

        try:
            try:
                driver.set_page_load_timeout(25)
            except Exception:
                pass

            original_handles = list(driver.window_handles)
            original_handle = driver.current_window_handle if original_handles else None

            driver.execute_script("window.open('about:blank', '_blank');")
            time.sleep(0.5)

            new_handles = [
                handle for handle in driver.window_handles
                if handle not in original_handles
            ]

            if not new_handles:
                raise RuntimeError("Could not open temporary timezone detection tab.")

            verification_handle = new_handles[-1]
            driver.switch_to.window(verification_handle)

            deadline = time.time() + float(timeout)

            while time.time() < deadline:
                for endpoint in geo_endpoints:
                    try:
                        geo_data = self._collect_geo_timezone_from_endpoint(driver, endpoint)
                        if not isinstance(geo_data, dict):
                            continue

                        geo_data["endpoint"] = endpoint
                        last_geo_data = geo_data

                        timezone_value = str(geo_data.get("timezone", "") or "").strip()
                        geo_ip_raw = str(geo_data.get("ip", "") or "").strip()
                        geo_ip = self._normalize_ip_value(geo_ip_raw)

                        if not timezone_value:
                            logger.warning(
                                f"Proxy timezone endpoint returned no timezone: {endpoint}"
                            )
                            continue

                        if expected_proxy_ip:
                            if geo_ip and geo_ip == expected_proxy_ip:
                                logger.info(
                                    f"Verified browser proxy route ready: "
                                    f"geo_ip={geo_ip} matched expected_proxy_ip={expected_proxy_ip}"
                                )
                                return timezone_value, geo_data

                            logger.warning(
                                "Browser proxy route not ready or IP mismatch during timezone sync: "
                                f"endpoint={endpoint} | geo_ip={geo_ip_raw or 'none'} | "
                                f"expected_proxy_ip={expected_proxy_ip}"
                            )
                            continue

                        logger.info(
                            "Proxy host is not a direct IP, accepting browser-context geo timezone: "
                            f"host={expected_proxy_host or 'unknown'} | "
                            f"geo_ip={geo_ip_raw or 'unknown'} | timezone={timezone_value}"
                        )
                        return timezone_value, geo_data

                    except Exception as e:
                        logger.warning(
                            f"Proxy timezone endpoint failed in browser context: {endpoint} | {e}"
                        )
                        continue

                time.sleep(float(retry_delay))

            logger.warning(
                "Verified proxy timezone route timed out. "
                f"expected_proxy_ip={expected_proxy_ip or 'not-direct-ip'} | "
                f"last_geo_ip={last_geo_data.get('ip', '')} | "
                f"last_timezone={last_geo_data.get('timezone', '')}"
            )
            return None, last_geo_data

        finally:
            try:
                if verification_handle and verification_handle in driver.window_handles:
                    driver.switch_to.window(verification_handle)
                    driver.close()
            except Exception as e:
                logger.warning(f"Temporary timezone detection tab close skipped: {e}")

            try:
                if original_handle and original_handle in driver.window_handles:
                    driver.switch_to.window(original_handle)
            except Exception as e:
                logger.warning(f"Original browser tab restore after timezone detection skipped: {e}")

    def _collect_geo_timezone_from_endpoint(self, driver, endpoint: str) -> Dict:
        driver.get(endpoint)
        time.sleep(1.2)

        try:
            body_text = driver.execute_script(
                "return document.body ? (document.body.innerText || document.body.textContent || '') : '';"
            )
        except Exception:
            body_text = ""

        body_text = str(body_text or "").strip()
        return self._extract_geo_timezone_from_text(body_text)

    def _extract_geo_timezone_from_text(self, text: str) -> Dict:
        if not text:
            return {}

        try:
            data = json.loads(text)
        except Exception:
            return {}

        if not isinstance(data, dict):
            return {}

        raw_timezone = data.get("timezone")
        timezone_value = ""

        if isinstance(raw_timezone, dict):
            timezone_value = str(
                raw_timezone.get("id")
                or raw_timezone.get("timezone")
                or raw_timezone.get("name")
                or ""
            ).strip()
        else:
            timezone_value = str(raw_timezone or "").strip()

        ip_value = (
            data.get("ip")
            or data.get("query")
            or data.get("address")
            or ""
        )

        return {
            "ip": str(ip_value or "").strip(),
            "timezone": timezone_value,
            "city": str(data.get("city", "") or ""),
            "region": str(data.get("region", "") or data.get("regionName", "") or ""),
            "country": str(data.get("country", "") or data.get("country_name", "") or ""),
            "country_code": str(data.get("country_code", "") or data.get("countryCode", "") or ""),
        }            
    
    def _inject_custom_js(self, driver, js_code: str):
        try:
            driver.execute_cdp_cmd('Page.addScriptToEvaluateOnNewDocument', {'source': js_code})
            logger.info("Custom JavaScript injection set for new documents")
        except Exception as e:
            logger.warning(f"Failed to inject custom JS: {e}")
            
    def _start_cdp_timezone_controller(
        self,
        driver,
        timezone_id: str,
        duration_seconds: int = 7200,
    ):
        """
        Browser-only timezone controller.

        Windows timezone is not changed.
        CDP timezone is applied to the current tab and newly opened tabs.
        """
        try:
            timezone_id = str(timezone_id or "").strip()
            if not timezone_id:
                return

            controller_thread = threading.Thread(
                target=self._cdp_timezone_controller_loop,
                args=(driver, timezone_id, int(duration_seconds)),
                daemon=True,
            )
            controller_thread.start()

            logger.info(
                f"Browser-only CDP timezone controller started for {timezone_id}"
            )

        except Exception as e:
            logger.warning(f"Failed to start CDP timezone controller: {e}")

    def _cdp_timezone_controller_loop(
        self,
        driver,
        timezone_id: str,
        duration_seconds: int,
    ):
        applied_handles = set()
        deadline = time.time() + max(60, int(duration_seconds))

        while time.time() < deadline:
            try:
                handles = list(driver.window_handles)
            except Exception:
                return

            new_handles = [
                handle for handle in handles
                if handle not in applied_handles
            ]

            if new_handles and timezone_id:
                try:
                    original_handle = driver.current_window_handle
                except Exception:
                    original_handle = None

                for handle in new_handles:
                    try:
                        if handle not in driver.window_handles:
                            applied_handles.add(handle)
                            continue

                        driver.switch_to.window(handle)

                        driver.execute_cdp_cmd(
                            'Emulation.setTimezoneOverride',
                            {'timezoneId': timezone_id}
                        )

                        applied_handles.add(handle)
                        logger.info(
                            f"CDP timezone applied to browser tab: {handle} | {timezone_id}"
                        )

                    except Exception as e:
                        applied_handles.add(handle)
                        logger.warning(
                            f"CDP timezone apply skipped for tab {handle}: {e}"
                        )

                try:
                    if original_handle and original_handle in driver.window_handles:
                        driver.switch_to.window(original_handle)
                except Exception:
                    pass

            time.sleep(0.75)            
    
    def update_chromium(self):
        base_path = os.path.join(os.path.dirname(__file__), "chromium")
        os.makedirs(base_path, exist_ok=True)
        
        api_url = "https://api.github.com/repos/ungoogled-software/ungoogled-chromium-windows/releases/latest"
        response = requests.get(api_url)
        if response.status_code != 200:
            logger.error("Failed to fetch latest release info from GitHub.")
            return
        latest_release = response.json()
        
        download_url = None
        for asset in latest_release.get('assets', []):
            if asset.get('name', '').endswith('windows_x64.zip'):
                download_url = asset.get('browser_download_url')
                break
        
        if not download_url:
            logger.error("No suitable Windows 64-bit asset found.")
            return
        
        zip_path = os.path.join(base_path, "chromium.zip")
        logger.info(f"Downloading Chromium from: {download_url}")
        response = requests.get(download_url, stream=True)
        response.raise_for_status()
        with open(zip_path, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)
        
        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            zip_ref.extractall(base_path)
        
        extracted_folders = [f for f in os.listdir(base_path) if f != "chromium.zip"]
        if len(extracted_folders) == 1:
            inner_path = os.path.join(base_path, extracted_folders[0])
            for item in os.listdir(inner_path):
                shutil.move(os.path.join(inner_path, item), base_path)
            os.rmdir(inner_path)
        
        os.remove(zip_path)
        self.chromium_path = os.path.join(base_path, "chrome.exe")
        logger.success("Portable Chromium downloaded and extracted successfully.")
    
    def _parse_proxy_config(self, proxy) -> Optional[Dict]:
        try:
            if isinstance(proxy, str):
                proxy = proxy.strip()
                if not proxy:
                    return None
                
                if '://' in proxy:
                    parsed = urlparse(proxy)
                    if not parsed.hostname or not parsed.port:
                        return None
                    
                    return {
                        "type": (parsed.scheme or "http").lower(),
                        "host": parsed.hostname,
                        "port": int(parsed.port),
                        "username": unquote(parsed.username) if parsed.username else None,
                        "password": unquote(parsed.password) if parsed.password else None
                    }
                
                parts = proxy.split(':')
                if len(parts) == 2:
                    return {
                        "type": "http",
                        "host": parts[0],
                        "port": int(parts[1]),
                        "username": None,
                        "password": None
                    }
                
                if len(parts) == 3:
                    return {
                        "type": "http",
                        "host": parts[0],
                        "port": int(parts[1]),
                        "username": parts[2],
                        "password": None
                    }
                
                if len(parts) >= 4:
                    host, port, user, password = parts[0], parts[1], parts[2], ':'.join(parts[3:])
                    return {
                        "type": "http",
                        "host": host,
                        "port": int(port),
                        "username": user,
                        "password": password
                    }
                
                return None
            
            if isinstance(proxy, dict):
                proxy_type = str(proxy.get('type', 'http')).lower()
                host = proxy.get('host', proxy.get('ip'))
                port = proxy.get('port')
                username = proxy.get('username', proxy.get('user'))
                password = proxy.get('password', proxy.get('pass'))
                
                if not host or not port:
                    return None
                
                return {
                    "type": proxy_type,
                    "host": host,
                    "port": int(port),
                    "username": username,
                    "password": password
                }
            
            return None
        except Exception as e:
            logger.warning(f"Error parsing proxy config: {e}")
            return None
    
    def _format_proxy_for_chrome(self, proxy_info: Dict) -> Optional[str]:
        try:
            proxy_type = str(proxy_info.get("type", "http")).lower()
            host = proxy_info.get("host")
            port = proxy_info.get("port")
            
            if proxy_type not in ("http", "https", "socks4", "socks5"):
                logger.warning(f"Unsupported proxy type: {proxy_type}")
                return None
            
            if not host or not port:
                return None
            
            return f"{proxy_type}://{host}:{port}"
        except Exception as e:
            logger.warning(f"Error formatting proxy for Chrome: {e}")
            return None
    def _start_socks5_auth_bridge(self, proxy_info: Dict) -> Tuple[str, int]:
        class ReusableThreadingTCPServer(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True
        
        launcher = self
        
        class Socks5AuthBridgeHandler(socketserver.BaseRequestHandler):
            def handle(self):
                launcher._handle_http_proxy_request(self.request, proxy_info)
        
        server = ReusableThreadingTCPServer(("127.0.0.1", 0), Socks5AuthBridgeHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        
        self._proxy_bridge_servers.append(server)
        host, port = server.server_address
        return host, port
    
    def _wait_for_local_proxy(self, host: str, port: int, timeout: float = 5.0):
        deadline = time.time() + timeout
        last_error = None
        
        while time.time() < deadline:
            try:
                test_sock = socket.create_connection((host, int(port)), timeout=0.5)
                test_sock.close()
                logger.info(f"Local proxy bridge is ready: {host}:{port}")
                return
            except OSError as e:
                last_error = e
                time.sleep(0.05)
        
        raise RuntimeError(f"Local proxy bridge did not become ready: {host}:{port} | {last_error}")
    
    def _handle_http_proxy_request(self, client_sock: socket.socket, proxy_info: Dict):
        remote_sock = None
        try:
            logger.info("SOCKS5 bridge accepted browser connection")
            client_sock.settimeout(30)
            request_data = b""
            
            while b"\r\n\r\n" not in request_data:
                chunk = client_sock.recv(4096)
                if not chunk:
                    return
                request_data += chunk
                if len(request_data) > 65536:
                    return
            
            header_data, body_data = request_data.split(b"\r\n\r\n", 1)
            header_text = header_data.decode("iso-8859-1", errors="replace")
            header_lines = header_text.split("\r\n")
            request_line = header_lines[0]
            request_parts = request_line.split()
            logger.info(f"SOCKS5 bridge received request: {request_line}")
            
            if len(request_parts) < 3:
                return
            
            method, target, version = request_parts[0].upper(), request_parts[1], request_parts[2]
            
            if method == "CONNECT":
                if ":" in target:
                    target_host, target_port = target.rsplit(":", 1)
                    target_port = int(target_port)
                else:
                    target_host = target
                    target_port = 443
                
                remote_sock = self._connect_remote_socks5(proxy_info, target_host, target_port)
                client_sock.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                
                if body_data:
                    remote_sock.sendall(body_data)
                
                self._relay_sockets(client_sock, remote_sock)
                return
            
            parsed = urlparse(target)
            if parsed.hostname:
                target_host = parsed.hostname
                target_port = parsed.port or (443 if parsed.scheme == "https" else 80)
                path = parsed.path or "/"
                if parsed.query:
                    path += f"?{parsed.query}"
            else:
                host_header = None
                for line in header_lines[1:]:
                    if line.lower().startswith("host:"):
                        host_header = line.split(":", 1)[1].strip()
                        break
                
                if not host_header:
                    return
                
                if ":" in host_header:
                    target_host, target_port = host_header.rsplit(":", 1)
                    target_port = int(target_port)
                else:
                    target_host = host_header
                    target_port = 80
                
                path = target
            
            remote_sock = self._connect_remote_socks5(proxy_info, target_host, target_port)
            
            remaining_headers = "\r\n".join(header_lines[1:])
            forwarded_header = f"{method} {path} {version}\r\n{remaining_headers}\r\n\r\n"
            remote_sock.sendall(forwarded_header.encode("iso-8859-1") + body_data)
            
            self._relay_sockets(client_sock, remote_sock)
        except Exception as e:
            logger.warning(f"SOCKS5 auth bridge request failed: {e}")
            try:
                client_sock.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            except Exception:
                pass
        finally:
            try:
                if remote_sock:
                    remote_sock.close()
            except Exception:
                pass
            try:
                client_sock.close()
            except Exception:
                pass
    
    def _connect_remote_socks5(self, proxy_info: Dict, target_host: str, target_port: int) -> socket.socket:
        proxy_host = proxy_info.get("host")
        proxy_port = int(proxy_info.get("port"))
        username = str(proxy_info.get("username") or "")
        password = str(proxy_info.get("password") or "")
        
        sock = socket.create_connection((proxy_host, proxy_port), timeout=30)
        sock.settimeout(30)
        
        if username or password:
            sock.sendall(b"\x05\x01\x02")
            response = self._recv_exact(sock, 2)
            if response != b"\x05\x02":
                raise RuntimeError("SOCKS5 proxy did not accept username/password authentication.")
            
            username_bytes = username.encode("utf-8")
            password_bytes = password.encode("utf-8")
            
            if len(username_bytes) > 255 or len(password_bytes) > 255:
                raise RuntimeError("SOCKS5 username/password is too long.")
            
            auth_packet = (
                b"\x01"
                + bytes([len(username_bytes)])
                + username_bytes
                + bytes([len(password_bytes)])
                + password_bytes
            )
            sock.sendall(auth_packet)
            
            auth_response = self._recv_exact(sock, 2)
            if auth_response != b"\x01\x00":
                raise RuntimeError("SOCKS5 username/password authentication failed.")
        else:
            sock.sendall(b"\x05\x01\x00")
            response = self._recv_exact(sock, 2)
            if response != b"\x05\x00":
                raise RuntimeError("SOCKS5 proxy did not accept no-auth connection.")
        
        port_bytes = int(target_port).to_bytes(2, "big")
        
        try:
            addr_bytes = socket.inet_aton(target_host)
            connect_packet = b"\x05\x01\x00\x01" + addr_bytes + port_bytes
        except OSError:
            host_bytes = target_host.encode("idna")
            if len(host_bytes) > 255:
                raise RuntimeError("Target hostname is too long for SOCKS5.")
            connect_packet = b"\x05\x01\x00\x03" + bytes([len(host_bytes)]) + host_bytes + port_bytes
        
        sock.sendall(connect_packet)
        
        reply = self._recv_exact(sock, 4)
        if reply[1] != 0x00:
            socks_errors = {
                1: "General SOCKS server failure",
                2: "Connection not allowed by ruleset",
                3: "Network unreachable",
                4: "Host unreachable",
                5: "Connection refused",
                6: "TTL expired",
                7: "Command not supported",
                8: "Address type not supported"
            }
            error_code = reply[1]
            error_meaning = socks_errors.get(error_code, "Unknown SOCKS5 error")
            raise RuntimeError(
                f"SOCKS5 connect failed for target {target_host}:{target_port} "
                f"via proxy {proxy_host}:{proxy_port} | "
                f"code={error_code} | meaning={error_meaning}"
            )
        
        atyp = reply[3]
        if atyp == 0x01:
            self._recv_exact(sock, 4)
        elif atyp == 0x03:
            domain_len = self._recv_exact(sock, 1)[0]
            self._recv_exact(sock, domain_len)
        elif atyp == 0x04:
            self._recv_exact(sock, 16)
        else:
            raise RuntimeError("SOCKS5 proxy returned invalid address type.")
        
        self._recv_exact(sock, 2)
        sock.settimeout(None)
        return sock
    
    def _recv_exact(self, sock: socket.socket, length: int) -> bytes:
        data = b""
        while len(data) < length:
            chunk = sock.recv(length - len(data))
            if not chunk:
                raise RuntimeError("Socket closed before expected data was received.")
            data += chunk
        return data
    
    def _relay_sockets(self, client_sock: socket.socket, remote_sock: socket.socket):
        sockets = [client_sock, remote_sock]
        try:
            for item in sockets:
                item.setblocking(True)
                item.settimeout(120)
            
            while True:
                readable, _, errored = select.select(sockets, [], sockets, 120)
                if errored:
                    return
                if not readable:
                    return
                
                for item in readable:
                    try:
                        data = item.recv(8192)
                        if not data:
                            return
                        
                        other = remote_sock if item is client_sock else client_sock
                        other.sendall(data)
                    except (socket.timeout, TimeoutError):
                        return
                    except OSError as e:
                        logger.warning(f"SOCKS5 bridge relay stopped: {e}")
                        return
        finally:
            try:
                remote_sock.close()
            except Exception:
                pass
            try:
                client_sock.close()
            except Exception:
                pass            
        
    def _create_proxy_auth_extension(self, extension_dir: str, proxy_info: Dict):
        os.makedirs(extension_dir, exist_ok=True)
        
        proxy_type = str(proxy_info.get("type", "http")).lower()
        host = str(proxy_info.get("host"))
        port = int(proxy_info.get("port"))
        username = str(proxy_info.get("username") or "")
        password = str(proxy_info.get("password") or "")
        
        has_auth = bool(username or password)
        permissions = ["proxy", "storage"]
        if has_auth:
            permissions.extend(["webRequest", "webRequestAuthProvider"])

        manifest_json = {
            "name": "JubraLogin Proxy Auth",
            "version": "1.0.0",
            "manifest_version": 3,
            "permissions": permissions,
            "host_permissions": [
                "<all_urls>"
            ],
            "background": {
                "service_worker": "background.js"
            }
        }

        background_js = f"""
const proxyConfig = {{
    mode: "fixed_servers",
    rules: {{
        singleProxy: {{
            scheme: {json.dumps(proxy_type)},
            host: {json.dumps(host)},
            port: {port}
        }},
        bypassList: ["localhost", "127.0.0.1"]
    }}
}};

chrome.proxy.settings.set({{ value: proxyConfig, scope: "regular" }}, function() {{}});
"""

        if has_auth:
            background_js += f"""
chrome.webRequest.onAuthRequired.addListener(
    function(details, asyncCallback) {{
        asyncCallback({{
            authCredentials: {{
                username: {json.dumps(username)},
                password: {json.dumps(password)}
            }}
        }});
    }},
    {{ urls: ["<all_urls>"] }},
    ["asyncBlocking"]
);
"""

        with open(os.path.join(extension_dir, "manifest.json"), "w", encoding="utf-8") as f:
            json.dump(manifest_json, f, indent=2)
        
        with open(os.path.join(extension_dir, "background.js"), "w", encoding="utf-8") as f:
            f.write(background_js)
    
    def _format_proxy_for_playwright(self, proxy) -> str:
        proxy_info = self._parse_proxy_config(proxy)
        if not proxy_info:
            return None
        return self._format_proxy_for_chrome(proxy_info)
    

    def _stable_seed_from_fingerprint(self, fingerprint: Dict, key: str, default: int = 123456789) -> int:
        """Return a deterministic 32-bit seed for stable per-profile browser noise."""
        try:
            if not isinstance(fingerprint, dict):
                return int(default)

            audio_data = fingerprint.get("audio", {}) if isinstance(fingerprint.get("audio", {}), dict) else {}
            direct_value = None
            if key == "canvas":
                direct_value = fingerprint.get("canvas_seed")
            elif key == "audio":
                direct_value = fingerprint.get("audio_seed") or audio_data.get("seed")
            elif key == "media":
                direct_value = fingerprint.get("media_seed")
            elif key == "fonts":
                direct_value = fingerprint.get("font_seed")

            if direct_value not in (None, ""):
                return max(1, int(float(direct_value))) & 0x7fffffff

            material = json.dumps({
                "key": key,
                "ua": fingerprint.get("userAgent") or fingerprint.get("user_agent"),
                "platform": fingerprint.get("platform"),
                "screen": fingerprint.get("screen"),
                "webgl": fingerprint.get("webgl"),
                "audio_device": audio_data.get("device_id") or audio_data.get("sinkId"),
            }, sort_keys=True, default=str)

            digest = hashlib.sha256(material.encode("utf-8", errors="ignore")).hexdigest()
            return max(1, int(digest[:8], 16)) & 0x7fffffff
        except Exception:
            return int(default)

    def _safe_js_number(self, value, default, minimum, maximum):
        try:
            number = int(float(value))
        except Exception:
            number = int(default)
        return max(int(minimum), min(int(maximum), number))

    def _font_profile_for_fingerprint(self, fingerprint: Dict) -> list:
        """Return a stable OS-based font list for browser-side checks."""
        try:
            fonts = fingerprint.get("fonts") if isinstance(fingerprint, dict) else None
            if isinstance(fonts, list) and fonts:
                return [str(font) for font in fonts if str(font).strip()][:40]

            os_value = str(
                fingerprint.get("os")
                or fingerprint.get("os_type")
                or fingerprint.get("platform_os")
                or "Windows"
            ).strip().lower()

            if os_value in ("mac", "macos", "osx", "darwin"):
                return [
                    "Arial", "Helvetica", "Helvetica Neue", "Menlo", "Monaco",
                    "San Francisco", "SF Pro Text", "SF Pro Display", "Avenir",
                    "Georgia", "Times New Roman", "Apple Color Emoji", "Courier"
                ]
            if os_value == "linux":
                return [
                    "DejaVu Sans", "DejaVu Serif", "DejaVu Sans Mono", "Liberation Sans",
                    "Liberation Serif", "Liberation Mono", "Noto Sans", "Noto Serif",
                    "Ubuntu", "Cantarell", "Droid Sans", "FreeSans"
                ]

            return [
                "Arial", "Calibri", "Cambria", "Candara", "Consolas", "Corbel",
                "Georgia", "Segoe UI", "Segoe UI Emoji", "Tahoma", "Times New Roman",
                "Trebuchet MS", "Verdana", "Courier New", "Microsoft Sans Serif"
            ]
        except Exception:
            return ["Arial", "Segoe UI", "Times New Roman", "Verdana"]

    def _media_devices_for_fingerprint(self, fingerprint: Dict, media_seed: int) -> list:
        """Return stable media device entries for enumerateDevices()."""
        try:
            devices = fingerprint.get("media_devices") if isinstance(fingerprint, dict) else None
            if isinstance(devices, list) and devices:
                clean = []
                for item in devices:
                    if not isinstance(item, dict):
                        continue
                    kind = str(item.get("kind") or "").strip()
                    if kind not in {"audioinput", "audiooutput", "videoinput"}:
                        continue
                    clean.append({
                        "kind": kind,
                        "label": str(item.get("label") or ""),
                        "deviceId": str(item.get("deviceId") or item.get("device_id") or ""),
                        "groupId": str(item.get("groupId") or item.get("group_id") or ""),
                    })
                if clean:
                    return clean[:6]

            os_value = str(
                fingerprint.get("os")
                or fingerprint.get("os_type")
                or fingerprint.get("platform_os")
                or "Windows"
            ).strip().lower()

            if os_value in ("mac", "macos", "osx", "darwin"):
                mic, speaker, camera = "MacBook Pro Microphone", "MacBook Pro Speakers", "FaceTime HD Camera"
            elif os_value == "linux":
                mic, speaker, camera = "Built-in Audio Analog Stereo", "Built-in Audio Analog Stereo", "Integrated Webcam"
            else:
                mic, speaker, camera = "Microphone Array (Realtek(R) Audio)", "Speakers (Realtek(R) Audio)", "Integrated Camera"

            base = f"{os_value}|{int(media_seed)}"
            digest = hashlib.sha256(base.encode("utf-8", errors="ignore")).hexdigest()
            group_audio = digest[:16]
            group_video = digest[16:32]
            return [
                {"kind": "audioinput", "label": mic, "deviceId": f"audioinput-{digest[:16]}", "groupId": group_audio},
                {"kind": "audiooutput", "label": speaker, "deviceId": f"audiooutput-{digest[8:24]}", "groupId": group_audio},
                {"kind": "videoinput", "label": camera, "deviceId": f"videoinput-{digest[16:32]}", "groupId": group_video},
            ]
        except Exception:
            return [
                {"kind": "audioinput", "label": "Microphone", "deviceId": "audioinput-default", "groupId": "audio-default"},
                {"kind": "audiooutput", "label": "Speakers", "deviceId": "audiooutput-default", "groupId": "audio-default"},
                {"kind": "videoinput", "label": "Integrated Camera", "deviceId": "videoinput-default", "groupId": "video-default"},
            ]

    def _get_extra_stealth_script(
        self,
        fingerprint: Dict,
        allow_timezone_override: bool = True,
        language_list: list = None,
    ) -> str:
        user_agent = str(
            fingerprint.get("userAgent")
            or fingerprint.get("user_agent")
            or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        platform_value = self._resolve_navigator_platform(fingerprint, user_agent)
        if language_list is None:
            language_list = self._resolve_language_list(fingerprint)
        else:
            language_list = [str(item).strip() for item in language_list if str(item).strip()]
        if not language_list:
            language_list = ["en-US", "en"]
        primary_language = language_list[0]
        logger.info(f"Extra stealth JS locale reused: languages={','.join(language_list)}")
        hardware_data = fingerprint.get("hardware", {}) if isinstance(fingerprint.get("hardware", {}), dict) else {}
        hardware_concurrency = self._safe_js_number(
            fingerprint.get("hardwareConcurrency") or hardware_data.get("hardwareConcurrency"),
            8,
            1,
            128,
        )
        device_memory = self._safe_js_number(
            fingerprint.get("deviceMemory") or hardware_data.get("deviceMemory"),
            8,
            1,
            128,
        )
        canvas_seed = self._stable_seed_from_fingerprint(fingerprint, "canvas", 123456789)
        audio_seed = self._stable_seed_from_fingerprint(fingerprint, "audio", 987654321)
        media_seed = self._stable_seed_from_fingerprint(fingerprint, "media", 246813579)
        font_seed = self._stable_seed_from_fingerprint(fingerprint, "fonts", 135792468)
        font_list = self._font_profile_for_fingerprint(fingerprint)
        media_devices = self._media_devices_for_fingerprint(fingerprint, media_seed)
        webrtc_mode = str(fingerprint.get("webrtc") or fingerprint.get("webrtc_mode") or "disabled").strip().lower()

        metadata = self._build_user_agent_metadata(user_agent, platform_value)

        # ------------------------------------------------------------------
        # Root-cause fix for external checkers (PixelScan, CreepJS, BrowserLeaks,
        # Whoer, amiunique, iphey, FingerprintJS Pro, etc.) reporting
        # "masking/emulation detected" even when navigator.platform / UA / WebGL
        # values are already correct for the selected Linux/macOS profile.
        #
        # Three independent detection vectors were found and are all fixed here,
        # in ONE shared "identity kit" IIFE that every later script block reuses
        # (all `script_lines` entries are joined with "\n" into a single script
        # string before injection, so top-level `const`/`function` declared here
        # are in scope for every block below - no `window.__jubra*` or
        # `prototype.__jubra*` pollution is needed anymore):
        #
        # 1) toString masking: overriding a getter/method with a plain JS
        #    function is functionally correct but
        #    Function.prototype.toString.call(fn) reveals JS source instead of
        #    "[native code]". Fixed by wrapping Function.prototype.toString in a
        #    Proxy (same technique selenium-stealth's utils.patchToString uses)
        #    so tagged replacement functions report as native.
        # 2) fn.name mismatch: a real native getter's function name is
        #    "get <prop>" (e.g. "get platform"), and a real native method's name
        #    is the bare method name (e.g. "getParameter"). A plain
        #    `function(x){...}` assigned via `obj.getParameter = function(x){}`
        #    has an EMPTY name, which is also checkable. Fixed by setting
        #    `.name` on every tagged function via the same `tag()` helper.
        # 3) Prototype/window pollution markers: previous "already patched"
        #    guards stored flags like `__jubraPatched` / `__jubraFontsPatched`
        #    directly as OWN PROPERTIES on native prototypes
        #    (Navigator.prototype, WebGLRenderingContext.prototype,
        #    AudioBuffer.prototype, ...) and on `window`. Any checker that runs
        #    `Object.getOwnPropertyNames(SomePrototype)` and looks for
        #    non-standard properties (CreepJS does exactly this) would see the
        #    literal string "jubra" and immediately flag automation/tooling.
        #    Fixed by moving every "already patched" flag into a private
        #    `WeakSet`/`WeakMap` inside this closure - nothing is ever written
        #    onto a native prototype or onto `window`.
        #
        # A separate, self-contained copy of the same toString/name masking is
        # used inside the Worker prelude further below, because a Worker has
        # its own global scope and cannot see this closure.
        # ------------------------------------------------------------------
        identity_kit_script = """
        // Shared native-identity kit - MUST run before any other stealth block.
        // Every following script block (joined into the same document script)
        // can call __jubraKit.tag(fn, 'method'|'getter', name) to make its
        // replacement report exactly like the real native implementation,
        // and __jubraKit.seen(obj) / __jubraKit.markSeen(obj) as a private
        // "already patched" guard that never touches the object itself.
        const __jubraKit = (() => {
            const nativeToStringText = Function.prototype.toString + '';
            const makeNativeText = (name) => nativeToStringText.replace('toString', name || '');
            const tagged = new WeakMap();
            const patchedGuard = new WeakSet();
            const originalFunctionToString = Function.prototype.toString;

            // Use a Symbol (not a string property name) for the "already installed"
            // marker. A string name like "__jubraToStringInstalled" would still show
            // up under Object.getOwnPropertyNames(Function.prototype), which is
            // exactly the kind of prototype-pollution scan CreepJS-style checkers
            // run. A Symbol only shows up under getOwnPropertySymbols() with a
            // description that reveals nothing checkers key off of, and per-realm
            // symbols naturally avoid double-patching within the same document.
            const installedMarker = Symbol.for('_sI');

            try {
                if (!Object.prototype.hasOwnProperty.call(Function.prototype, installedMarker)) {
                    const toStringProxy = new Proxy(originalFunctionToString, {
                        apply(target, ctx, args) {
                            if (ctx === originalFunctionToString) {
                                return makeNativeText('toString');
                            }
                            if (tagged.has(ctx)) {
                                return tagged.get(ctx);
                            }
                            return Reflect.apply(target, ctx, args);
                        }
                    });
                    Object.defineProperty(Function.prototype, 'toString', {
                        value: toStringProxy,
                        writable: true,
                        configurable: true
                    });
                    Object.defineProperty(Function.prototype, installedMarker, {
                        value: true,
                        configurable: true
                    });
                }
            } catch (e) {}

            // kind: 'getter' -> function name becomes "get <name>" (matches real
            // accessor descriptors); 'method' -> function name stays "<name>"
            // (matches real prototype methods/constructors).
            const tag = (fn, kind, name) => {
                try {
                    const label = kind === 'getter' ? ('get ' + name) : name;
                    Object.defineProperty(fn, 'name', { value: label, configurable: true });
                    tagged.set(fn, makeNativeText(label));
                } catch (e) {}
                return fn;
            };

            // FIX: Use data descriptor {value, writable, configurable} instead of
            // getter descriptor {get, configurable}. A getter descriptor is detectable
            // by checkers like IPhey via Object.getOwnPropertyDescriptor - they check
            // if the property has a 'get' key instead of a 'value' key. Native browser
            // properties like navigator.userAgent use data descriptors, so we must too.
            // The tag() wrapper ensures Function.prototype.toString reports [native code].
            const defineNativeGetter = (target, prop, value) => {
                try {
                    const taggedValue = (typeof value === 'function')
                        ? tag(value, 'method', prop)
                        : value;
                    Object.defineProperty(target, prop, {
                        value: taggedValue,
                        writable: true,
                        configurable: true,
                        enumerable: true,
                    });
                } catch (e) {}
            };

            return {
                tag,
                defineNativeGetter,
                seen: (obj) => patchedGuard.has(obj),
                markSeen: (obj) => { try { patchedGuard.add(obj); } catch (e) {} },
            };
        })();
        """

        script_lines = [
            "// Extra Stealth Layer - JubraLogin",
            identity_kit_script,
            f"""
            (() => {{
                __jubraKit.defineNativeGetter(navigator, 'webdriver', undefined);

                // Realistic Chrome PluginArray: 5 default plugins that real Chrome ships.
                // DO NOT set navigator.plugins to [1,2,3,4,5] - that's an instant
                // detection vector (IPhey "window property") because real plugins
                // must be Plugin objects inside a PluginArray, not plain numbers.
                const __jubraPluginArray = (() => {{
                    // Build minimal Plugin-like objects that pass basic type checks.
                    // Real Chrome 149 ships these 5 plugins:
                    const pluginData = [
                        {{ name: 'PDF Viewer', filename: 'internal-pdf-viewer', description: 'Portable Document Format', mimeTypes: ['application/pdf'] }},
                        {{ name: 'Chrome PDF Viewer', filename: 'internal-pdf-viewer', description: 'Portable Document Format', mimeTypes: ['application/pdf'] }},
                        {{ name: 'Chromium PDF Viewer', filename: 'internal-pdf-viewer', description: 'Portable Document Format', mimeTypes: ['application/pdf'] }},
                        {{ name: 'Microsoft Edge PDF Viewer', filename: 'internal-pdf-viewer', description: 'Portable Document Format', mimeTypes: ['application/pdf'] }},
                        {{ name: 'WebKit built-in PDF', filename: 'internal-pdf-viewer', description: 'Portable Document Format', mimeTypes: ['application/pdf'] }},
                    ];
                    const arr = Object.create(PluginArray.prototype);
                    for (let i = 0; i < pluginData.length; i++) {{
                        const p = Object.create(Plugin.prototype);
                        Object.defineProperty(p, 'name', {{ value: pluginData[i].name, enumerable: true }});
                        Object.defineProperty(p, 'filename', {{ value: pluginData[i].filename, enumerable: true }});
                        Object.defineProperty(p, 'description', {{ value: pluginData[i].description, enumerable: true }});
                        Object.defineProperty(p, 'length', {{ value: pluginData[i].mimeTypes.length, enumerable: true }});
                        Object.defineProperty(arr, i, {{ value: p, enumerable: true }});
                        Object.defineProperty(arr, pluginData[i].name, {{ value: p, enumerable: false }});
                    }}
                    Object.defineProperty(arr, 'length', {{ value: pluginData.length, enumerable: true }});
                    return arr;
                }})();
                __jubraKit.defineNativeGetter(navigator, 'plugins', __jubraPluginArray);
            }})();
            """,
            ""
            f"""
            // Runtime identity sync: UA, platform, language and hardware.
            (() => {{
                const __jubraUserAgent = {json.dumps(user_agent)};
                const __jubraPlatform = {json.dumps(platform_value)};
                const __jubraLanguages = Object.freeze({json.dumps(language_list)});
                const __jubraLanguage = {json.dumps(primary_language)};
                const __jubraHardwareConcurrency = {hardware_concurrency};
                const __jubraDeviceMemory = {device_memory};
                const __jubraUAData = {json.dumps(metadata)};

                const navigatorTargets = [Navigator.prototype, navigator];
                for (const target of navigatorTargets) {{
                    __jubraKit.defineNativeGetter(target, 'userAgent', __jubraUserAgent);
                    __jubraKit.defineNativeGetter(target, 'platform', __jubraPlatform);
                    __jubraKit.defineNativeGetter(target, 'language', __jubraLanguage);
                    __jubraKit.defineNativeGetter(target, 'languages', __jubraLanguages);
                    __jubraKit.defineNativeGetter(target, 'hardwareConcurrency', __jubraHardwareConcurrency);
                    __jubraKit.defineNativeGetter(target, 'deviceMemory', __jubraDeviceMemory);
                }}

                const uaDataValue = {{
                    brands: __jubraUAData.brands,
                    mobile: __jubraUAData.mobile,
                    platform: __jubraUAData.platform,
                    getHighEntropyValues: __jubraKit.tag(async (hints) => {{
                        const full = {{
                            brands: __jubraUAData.brands,
                            fullVersionList: __jubraUAData.fullVersionList,
                            mobile: __jubraUAData.mobile,
                            platform: __jubraUAData.platform,
                            platformVersion: __jubraUAData.platformVersion,
                            architecture: __jubraUAData.architecture,
                            bitness: __jubraUAData.bitness,
                            model: __jubraUAData.model,
                            uaFullVersion: __jubraUAData.fullVersion,
                            fullVersion: __jubraUAData.fullVersion,
                            wow64: __jubraUAData.wow64
                        }};

                        if (!Array.isArray(hints)) {{
                            return full;
                        }}

                        const picked = {{}};
                        for (const hint of hints) {{
                            if (Object.prototype.hasOwnProperty.call(full, hint)) {{
                                picked[hint] = full[hint];
                            }}
                        }}
                        picked.brands = full.brands;
                        picked.mobile = full.mobile;
                        picked.platform = full.platform;
                        return picked;
                    }}, 'method', 'getHighEntropyValues'),
                    toJSON: __jubraKit.tag(() => {{
                        return {{
                            brands: __jubraUAData.brands,
                            mobile: __jubraUAData.mobile,
                            platform: __jubraUAData.platform
                        }};
                    }}, 'method', 'toJSON')
                }};

                for (const target of navigatorTargets) {{
                    __jubraKit.defineNativeGetter(target, 'userAgentData', uaDataValue);
                }}
            }})();
            """
        ]

        canvas_noise = fingerprint.get('canvas_noise')
        if canvas_noise and canvas_noise != 'None':
            canvas_noise_level = str(canvas_noise or "Medium")
            script_lines.append(f"""
            // Stable canvas noise: deterministic per profile, never Math.random per call.
            (() => {{
                const __jubraCanvasSeed = {int(canvas_seed)};
                const __jubraCanvasNoiseLevel = {json.dumps(canvas_noise_level)};
                const __jubraCanvasNoiseScale = ({{
                    Low: 0.35,
                    Medium: 0.70,
                    High: 1.10
                }})[__jubraCanvasNoiseLevel] || 0;

                if (!__jubraCanvasNoiseScale || __jubraKit.seen(HTMLCanvasElement.prototype)) {{
                    return;
                }}
                __jubraKit.markSeen(HTMLCanvasElement.prototype);

                const stableHash = (label) => {{
                    let h = (__jubraCanvasSeed ^ 2166136261) >>> 0;
                    const text = String(label || '');
                    for (let i = 0; i < text.length; i++) {{
                        h ^= text.charCodeAt(i);
                        h = Math.imul(h, 16777619);
                    }}
                    h ^= h >>> 13;
                    h = Math.imul(h, 1274126177);
                    return h >>> 0;
                }};

                const stableSigned = (label) => ((stableHash(label) % 2001) / 1000) - 1;

                const originalGetContext = HTMLCanvasElement.prototype.getContext;
                const originalToDataURL = HTMLCanvasElement.prototype.toDataURL;
                const originalToBlob = HTMLCanvasElement.prototype.toBlob;
                const patchedCanvasContexts = new WeakSet();

                const applyStableCanvasPixelNoise = (canvas) => {{
                    try {{
                        if (!canvas || patchedCanvasContexts.has(canvas)) {{
                            return;
                        }}
                        const ctx = originalGetContext.call(canvas, '2d', {{ willReadFrequently: true }});
                        if (!ctx || !canvas.width || !canvas.height) {{
                            return;
                        }}
                        const x = stableHash('pixel:x:' + canvas.width + ':' + canvas.height) % Math.max(1, canvas.width);
                        const y = stableHash('pixel:y:' + canvas.width + ':' + canvas.height) % Math.max(1, canvas.height);
                        const imageData = ctx.getImageData(x, y, 1, 1);
                        const delta = stableSigned('pixel:delta:' + x + ':' + y) >= 0 ? 1 : -1;
                        imageData.data[0] = Math.max(0, Math.min(255, imageData.data[0] + delta));
                        imageData.data[1] = Math.max(0, Math.min(255, imageData.data[1] - delta));
                        ctx.putImageData(imageData, x, y);
                        patchedCanvasContexts.add(canvas);
                    }} catch (e) {{}}
                }};

                const patchedRenderingContexts = new WeakSet();

                HTMLCanvasElement.prototype.getContext = __jubraKit.tag(function(type, ...args) {{
                    const ctx = originalGetContext.call(this, type, ...args);
                    if (!ctx || String(type).toLowerCase() !== '2d' || patchedRenderingContexts.has(ctx)) {{
                        return ctx;
                    }}

                    try {{
                        const originalFillText = ctx.fillText;
                        const originalStrokeText = ctx.strokeText;
                        const originalGetImageData = ctx.getImageData;

                        ctx.fillText = __jubraKit.tag(function(text, x, y, maxWidth) {{
                            const label = 'fill:' + text + ':' + x + ':' + y + ':' + (maxWidth || '');
                            const nx = x + stableSigned(label + ':x') * __jubraCanvasNoiseScale;
                            const ny = y + stableSigned(label + ':y') * __jubraCanvasNoiseScale;
                            if (arguments.length >= 4) {{
                                return originalFillText.call(this, text, nx, ny, maxWidth);
                            }}
                            return originalFillText.call(this, text, nx, ny);
                        }}, 'method', 'fillText');

                        ctx.strokeText = __jubraKit.tag(function(text, x, y, maxWidth) {{
                            const label = 'stroke:' + text + ':' + x + ':' + y + ':' + (maxWidth || '');
                            const nx = x + stableSigned(label + ':x') * __jubraCanvasNoiseScale;
                            const ny = y + stableSigned(label + ':y') * __jubraCanvasNoiseScale;
                            if (arguments.length >= 4) {{
                                return originalStrokeText.call(this, text, nx, ny, maxWidth);
                            }}
                            return originalStrokeText.call(this, text, nx, ny);
                        }}, 'method', 'strokeText');

                        ctx.getImageData = __jubraKit.tag(function(sx, sy, sw, sh) {{
                            applyStableCanvasPixelNoise(this.canvas);
                            return originalGetImageData.call(this, sx, sy, sw, sh);
                        }}, 'method', 'getImageData');

                        patchedRenderingContexts.add(ctx);
                    }} catch (e) {{}}

                    return ctx;
                }}, 'method', 'getContext');

                HTMLCanvasElement.prototype.toDataURL = __jubraKit.tag(function(...args) {{
                    applyStableCanvasPixelNoise(this);
                    return originalToDataURL.apply(this, args);
                }}, 'method', 'toDataURL');

                HTMLCanvasElement.prototype.toBlob = __jubraKit.tag(function(callback, ...args) {{
                    applyStableCanvasPixelNoise(this);
                    return originalToBlob.call(this, callback, ...args);
                }}, 'method', 'toBlob');
            }})();
            """)

        webgl_method = fingerprint.get('webgl_method', 'WebGL 2')
        webgl_data = fingerprint.get("webgl", {}) if isinstance(fingerprint.get("webgl", {}), dict) else {}
        vendor_str = str(webgl_data.get("vendor", "Google Inc."))
        renderer_str = str(webgl_data.get("renderer", "ANGLE (NVIDIA, NVIDIA GeForce RTX 4070, Direct3D11 vs_5_0)"))
        renderer_lower = renderer_str.lower()
        high_gpu_tokens = [
            "rtx 3070", "rtx 3080", "rtx 4070", "rtx 4080",
            "rx 6800", "rx 6900", "rx 7700", "rx 7800",
            "rtx a4000", "rtx a5000", "m1 max", "m2 max", "m3 pro",
        ]
        mid_gpu_tokens = [
            "rtx 2060", "rtx 3060", "gtx 1660", "rx 5700", "rx 6600",
            "iris", "apple m1", "apple m2", "apple m3",
        ]
        max_texture_size = 16384 if any(token in renderer_lower for token in high_gpu_tokens + mid_gpu_tokens) else 8192
        max_combined_texture_units = 32 if max_texture_size >= 16384 else 16
        script_lines.append(f"""
        // WebGL method: {webgl_method}
        (() => {{
            const __jubraWebGLVendor = {json.dumps(vendor_str)};
            const __jubraWebGLRenderer = {json.dumps(renderer_str)};
            const __jubraMaxTextureSize = {int(max_texture_size)};
            const __jubraMaxCombinedTextureUnits = {int(max_combined_texture_units)};
            const __debugRendererInfo = Object.freeze({{
                UNMASKED_VENDOR_WEBGL: 37445,
                UNMASKED_RENDERER_WEBGL: 37446
            }});

            const patchWebGLContext = (contextName, isWebGL2) => {{
                const context = window[contextName];
                if (!context || !context.prototype || __jubraKit.seen(context.prototype)) {{
                    return;
                }}

                const originalGetParameter = context.prototype.getParameter;
                const originalGetExtension = context.prototype.getExtension;
                const originalGetSupportedExtensions = context.prototype.getSupportedExtensions;

                __jubraKit.markSeen(context.prototype);

                context.prototype.getParameter = __jubraKit.tag(function(parameter) {{
                    switch (parameter) {{
                        case 37445: // UNMASKED_VENDOR_WEBGL
                            return __jubraWebGLVendor;
                        case 37446: // UNMASKED_RENDERER_WEBGL
                            return __jubraWebGLRenderer;
                        case 7936: // VENDOR
                            return 'WebKit';
                        case 7937: // RENDERER
                            return 'WebKit WebGL';
                        case 7938: // VERSION
                            return isWebGL2
                                ? 'WebGL 2.0 (OpenGL ES 3.0 Chromium)'
                                : 'WebGL 1.0 (OpenGL ES 2.0 Chromium)';
                        case 35724: // SHADING_LANGUAGE_VERSION
                            return isWebGL2
                                ? 'WebGL GLSL ES 3.00 (OpenGL ES GLSL ES 3.0 Chromium)'
                                : 'WebGL GLSL ES 1.0 (OpenGL ES GLSL ES 1.0 Chromium)';
                        case 3379: // MAX_TEXTURE_SIZE
                        case 34024: // MAX_RENDERBUFFER_SIZE
                            return __jubraMaxTextureSize;
                        case 34921: // MAX_VERTEX_ATTRIBS
                            return 16;
                        case 34930: // MAX_TEXTURE_IMAGE_UNITS
                        case 35660: // MAX_VERTEX_TEXTURE_IMAGE_UNITS
                            return 16;
                        case 35661: // MAX_COMBINED_TEXTURE_IMAGE_UNITS
                            return __jubraMaxCombinedTextureUnits;
                        case 36347: // MAX_VERTEX_UNIFORM_VECTORS
                        case 36349: // MAX_FRAGMENT_UNIFORM_VECTORS
                            return 1024;
                        default:
                            return originalGetParameter.call(this, parameter);
                    }}
                }}, 'method', 'getParameter');

                context.prototype.getExtension = __jubraKit.tag(function(name) {{
                    if (String(name || '').toLowerCase() === 'webgl_debug_renderer_info') {{
                        return __debugRendererInfo;
                    }}
                    return originalGetExtension ? originalGetExtension.call(this, name) : null;
                }}, 'method', 'getExtension');

                context.prototype.getSupportedExtensions = __jubraKit.tag(function() {{
                    const list = originalGetSupportedExtensions
                        ? (originalGetSupportedExtensions.call(this) || [])
                        : [];
                    if (list.indexOf('WEBGL_debug_renderer_info') === -1) {{
                        return list.concat(['WEBGL_debug_renderer_info']);
                    }}
                    return list;
                }}, 'method', 'getSupportedExtensions');
            }};

            patchWebGLContext('WebGLRenderingContext', false);
            patchWebGLContext('WebGL2RenderingContext', true);
        }})();
        """)

        # --- Web Worker identity propagation (PixelScan "masking detected" fix) ---
        # Root cause: Page.addScriptToEvaluateOnNewDocument (and every main-thread
        # JS hook above) never executes inside Web Workers. Strict checkers such
        # as PixelScan spawn a Worker + OffscreenCanvas and read the REAL
        # navigator.platform and the REAL WebGL UNMASKED_RENDERER from the worker
        # scope, bypassing the main-thread spoof. On emulated Linux/macOS/Android/
        # iOS profiles this leaked the true Windows platform ("Win32") and the true
        # Direct3D GPU, so PixelScan reported "masking detected" even though the
        # main thread (and therefore every other checker) looked correct.
        # Fix: wrap the Worker constructor so a prelude re-applies the SAME
        # emulated navigator + WebGL identity inside each worker before the site's
        # worker script runs. For Windows profiles the values equal the real ones,
        # so nothing changes. Any failure falls back to the native Worker.
        #
        # The worker has its own global scope (cannot see the page's __jubraKit
        # closure), so this prelude carries a compact, self-contained copy of the
        # same toString + name masking, and keeps its "already patched" flag in a
        # local variable instead of a property on WorkerGlobalScope/self.
        worker_prelude = (
            "(function(){try{"
            "var P=" + json.dumps(platform_value) + ";"
            "var UA=" + json.dumps(user_agent) + ";"
            "var LANGS=" + json.dumps(list(language_list)) + ";"
            "var LANG=" + json.dumps(primary_language) + ";"
            "var HC=" + json.dumps(hardware_concurrency) + ";"
            "var DM=" + json.dumps(device_memory) + ";"
            "var VEN=" + json.dumps(vendor_str) + ";"
            "var REN=" + json.dumps(renderer_str) + ";"
            "var MTS=" + json.dumps(int(max_texture_size)) + ";"
            "var MCTU=" + json.dumps(int(max_combined_texture_units)) + ";"
            "var nStr=Function.prototype.toString+'';"
            "var mkNat=function(n){return nStr.replace('toString',n||'');};"
            "var tagMap=(typeof WeakMap!=='undefined')?new WeakMap():null;"
            "var patchedSet=(typeof WeakSet!=='undefined')?new WeakSet():null;"
            "var origToStr=Function.prototype.toString;"
            "var toStrMarker=Symbol.for('_sI');"
            "try{if(!Object.prototype.hasOwnProperty.call(Function.prototype,toStrMarker)){"
            "var toStrProxy=new Proxy(origToStr,{apply:function(t,ctx,args){"
            "if(ctx===origToStr){return mkNat('toString');}"
            "if(tagMap&&tagMap.has(ctx)){return tagMap.get(ctx);}"
            "return Reflect.apply(t,ctx,args);}});"
            "Object.defineProperty(Function.prototype,'toString',{value:toStrProxy,writable:true,configurable:true});"
            "Object.defineProperty(Function.prototype,toStrMarker,{value:true,configurable:true});"
            "}}catch(e){}"
            "var tagNat=function(fn,kind,name){try{var label=kind==='getter'?('get '+name):name;"
            "Object.defineProperty(fn,'name',{value:label,configurable:true});"
            "if(tagMap){tagMap.set(fn,mkNat(label));}}catch(e){}return fn;};"
            "var def=function(o,p,v){try{Object.defineProperty(o,p,{value:v,writable:true,configurable:true,enumerable:true});}catch(e){}};"
            "try{var t=[];if(typeof WorkerNavigator!=='undefined'&&WorkerNavigator.prototype){t.push(WorkerNavigator.prototype);}"
            "if(typeof navigator!=='undefined'){t.push(navigator);}"
            "for(var i=0;i<t.length;i++){def(t[i],'platform',P);def(t[i],'userAgent',UA);def(t[i],'language',LANG);"
            "def(t[i],'languages',Object.freeze(LANGS.slice()));def(t[i],'hardwareConcurrency',HC);def(t[i],'deviceMemory',DM);}}catch(e){}"
            "var pw=function(n,w2){try{var C=self[n];if(!C||!C.prototype||(patchedSet&&patchedSet.has(C.prototype))){return;}"
            "var g=C.prototype.getParameter;if(patchedSet){patchedSet.add(C.prototype);}"
            "C.prototype.getParameter=tagNat(function(p){switch(p){case 37445:return VEN;case 37446:return REN;"
            "case 7936:return 'WebKit';case 7937:return 'WebKit WebGL';"
            "case 7938:return w2?'WebGL 2.0 (OpenGL ES 3.0 Chromium)':'WebGL 1.0 (OpenGL ES 2.0 Chromium)';"
            "case 3379:case 34024:return MTS;case 35661:return MCTU;default:return g.call(this,p);}},'method','getParameter');"
            "var ge=C.prototype.getExtension;C.prototype.getExtension=tagNat(function(nm){"
            "if(String(nm||'').toLowerCase()==='webgl_debug_renderer_info'){return Object.freeze({UNMASKED_VENDOR_WEBGL:37445,UNMASKED_RENDERER_WEBGL:37446});}"
            "return ge?ge.call(this,nm):null;},'method','getExtension');"
            "}catch(e){}};"
            "pw('WebGLRenderingContext',false);pw('WebGL2RenderingContext',true);"
            "}catch(e){}})();"
        )
        script_lines.append("""
        // Propagate the emulated identity into Web Worker contexts.
        (() => {
            try {
                // Symbol-based marker (see __jubraKit above): avoids a literal
                // "__jubraWorkerWrapped" own-property name showing up on the
                // global Worker constructor under Object.getOwnPropertyNames(Worker).
                const __jubraWorkerWrappedMarker = Symbol.for('_wW');
                if (typeof Worker === 'undefined' || Worker[__jubraWorkerWrappedMarker]) { return; }
                const __jubraWorkerPrelude = %s;
                const __JubraOriginalWorker = Worker;
                const __jubraWrapWorker = __jubraKit.tag(function(scriptURL, options) {
                    try {
                        if (options && options.type === 'module') {
                            return new __JubraOriginalWorker(scriptURL, options);
                        }
                        if (scriptURL instanceof URL) { scriptURL = scriptURL.href; }
                        let abs = '' + scriptURL;
                        try { abs = new URL(scriptURL, self.location.href).href; } catch (e) {}
                        const src = __jubraWorkerPrelude + '\\nimportScripts(' + JSON.stringify(abs) + ');';
                        const blob = new Blob([src], { type: 'application/javascript' });
                        const blobURL = URL.createObjectURL(blob);
                        return new __JubraOriginalWorker(blobURL, options);
                    } catch (e) {
                        return new __JubraOriginalWorker(scriptURL, options);
                    }
                }, 'method', 'Worker');
                try { __jubraWrapWorker.prototype = __JubraOriginalWorker.prototype; } catch (e) {}
                try { Object.defineProperty(__jubraWrapWorker, __jubraWorkerWrappedMarker, { value: true, configurable: true }); } catch (e) {}
                try {
                    Object.defineProperty(self, 'Worker', { value: __jubraWrapWorker, configurable: true, writable: true });
                } catch (e) {
                    self.Worker = __jubraWrapWorker;
                }
            } catch (e) {}
        })();
        """ % json.dumps(worker_prelude))

        audio_data = fingerprint.get('audio', {}) if isinstance(fingerprint.get('audio', {}), dict) else {}
        audio_latency = audio_data.get('latency') or audio_data.get('baseLatency')
        audio_device = audio_data.get('device_id') or audio_data.get('sinkId')
        audio_latency_value = 0.015
        try:
            if audio_latency not in (None, ""):
                audio_latency_value = float(audio_latency)
        except Exception:
            audio_latency_value = 0.015

        script_lines.append(f"""
        // Stable audio fingerprint layer: deterministic per profile, never Math.random per call.
        (() => {{
            const __jubraAudioSeed = {int(audio_seed)};
            const __jubraAudioLatency = {audio_latency_value};
            const __jubraAudioDeviceId = {json.dumps(str(audio_device)) if audio_device else 'undefined'};

            if (__jubraKit.seen(window)) {{
                return;
            }}
            __jubraKit.markSeen(window);

            const stableHash = (label) => {{
                let h = (__jubraAudioSeed ^ 2166136261) >>> 0;
                const text = String(label || '');
                for (let i = 0; i < text.length; i++) {{
                    h ^= text.charCodeAt(i);
                    h = Math.imul(h, 16777619);
                }}
                h ^= h >>> 13;
                h = Math.imul(h, 1274126177);
                return h >>> 0;
            }};

            const stableSigned = (label) => ((stableHash(label) % 2001) / 1000) - 1;

            try {{
                const AudioContextClass = window.AudioContext || window.webkitAudioContext;
                if (AudioContextClass && AudioContextClass.prototype) {{
                    try {{
                        __jubraKit.defineNativeGetter(AudioContextClass.prototype, 'baseLatency', __jubraAudioLatency);
                    }} catch (e) {{}}
                    try {{
                        __jubraKit.defineNativeGetter(AudioContextClass.prototype, 'outputLatency', Math.max(0.008, __jubraAudioLatency + 0.003));
                    }} catch (e) {{}}
                }}
            }} catch (e) {{}}

            try {{
                if (window.AudioBuffer && AudioBuffer.prototype && !__jubraKit.seen(AudioBuffer.prototype)) {{
                    __jubraKit.markSeen(AudioBuffer.prototype);
                    const originalGetChannelData = AudioBuffer.prototype.getChannelData;
                    const noisedBuffers = new WeakMap();
                    AudioBuffer.prototype.getChannelData = __jubraKit.tag(function(channel) {{
                        const data = originalGetChannelData.call(this, channel);
                        try {{
                            let doneChannels = noisedBuffers.get(data);
                            if (!doneChannels) {{
                                doneChannels = new Set();
                                noisedBuffers.set(data, doneChannels);
                            }}
                            if (!doneChannels.has(channel)) {{
                                doneChannels.add(channel);
                                const step = Math.max(97, Math.floor(data.length / 64));
                                for (let i = channel || 0; i < data.length; i += step) {{
                                    data[i] = data[i] + stableSigned('buffer:' + channel + ':' + i) * 0.00000012;
                                }}
                            }}
                        }} catch (e) {{}}
                        return data;
                    }}, 'method', 'getChannelData');
                }}
            }} catch (e) {{}}

            try {{
                if (window.AnalyserNode && AnalyserNode.prototype && !__jubraKit.seen(AnalyserNode.prototype)) {{
                    __jubraKit.markSeen(AnalyserNode.prototype);
                    const originalGetFloatFrequencyData = AnalyserNode.prototype.getFloatFrequencyData;
                    const originalGetByteFrequencyData = AnalyserNode.prototype.getByteFrequencyData;

                    AnalyserNode.prototype.getFloatFrequencyData = __jubraKit.tag(function(array) {{
                        const result = originalGetFloatFrequencyData.call(this, array);
                        try {{
                            for (let i = 0; i < array.length; i += 16) {{
                                array[i] = array[i] + stableSigned('floatFreq:' + i) * 0.00009;
                            }}
                        }} catch (e) {{}}
                        return result;
                    }}, 'method', 'getFloatFrequencyData');

                    AnalyserNode.prototype.getByteFrequencyData = __jubraKit.tag(function(array) {{
                        const result = originalGetByteFrequencyData.call(this, array);
                        try {{
                            for (let i = 0; i < array.length; i += 16) {{
                                const delta = stableSigned('byteFreq:' + i) >= 0 ? 1 : -1;
                                array[i] = Math.max(0, Math.min(255, array[i] + delta));
                            }}
                        }} catch (e) {{}}
                        return result;
                    }}, 'method', 'getByteFrequencyData');
                }}
            }} catch (e) {{}}

            try {{
                if (navigator.mediaDevices && navigator.mediaDevices.getUserMedia && !__jubraKit.seen(navigator.mediaDevices)) {{
                    __jubraKit.markSeen(navigator.mediaDevices);
                    const originalGetUserMedia = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
                    navigator.mediaDevices.getUserMedia = __jubraKit.tag(function(constraints) {{
                        const nextConstraints = Object.assign({{}}, constraints || {{}});
                        if (nextConstraints.audio) {{
                            const audioConstraints = typeof nextConstraints.audio === 'object'
                                ? Object.assign({{}}, nextConstraints.audio)
                                : {{}};
                            audioConstraints.latency = __jubraAudioLatency;
                            if (__jubraAudioDeviceId) {{
                                audioConstraints.deviceId = __jubraAudioDeviceId;
                            }}
                            nextConstraints.audio = audioConstraints;
                        }}
                        return originalGetUserMedia(nextConstraints);
                    }}, 'method', 'getUserMedia');
                }}
            }} catch (e) {{}}
        }})();
        """)

        script_lines.append(f"""
        // WebRTC, media devices and font consistency layer.
        (() => {{
            const __jubraWebRTCMode = {json.dumps(webrtc_mode)};
            const __jubraMediaSeed = {int(media_seed)};
            const __jubraFontSeed = {int(font_seed)};
            const __jubraFonts = Object.freeze({json.dumps(font_list)});
            const __jubraMediaDevices = Object.freeze({json.dumps(media_devices)});

            const stableHash = (label) => {{
                let h = (__jubraMediaSeed ^ 2166136261) >>> 0;
                const text = String(label || '');
                for (let i = 0; i < text.length; i++) {{
                    h ^= text.charCodeAt(i);
                    h = Math.imul(h, 16777619);
                }}
                h ^= h >>> 13;
                h = Math.imul(h, 1274126177);
                return h >>> 0;
            }};

            const makeDeviceInfo = (device) => {{
                try {{
                    return {{
                        kind: device.kind,
                        label: device.label || '',
                        deviceId: device.deviceId || ('device-' + stableHash(device.kind + device.label)),
                        groupId: device.groupId || ('group-' + stableHash('group:' + device.kind))
                    }};
                }} catch (e) {{
                    return device;
                }}
            }};

            const jubraEnumerateDevices = __jubraKit.tag(async function() {{
                try {{
                    return __jubraMediaDevices.map(makeDeviceInfo);
                }} catch (e) {{
                    return [];
                }}
            }}, 'method', 'enumerateDevices');

            const installJubraMediaDevicesPatch = () => {{
                try {{
                    const media = navigator.mediaDevices;
                    if (media && !__jubraKit.seen(media)) {{
                        __jubraKit.markSeen(media);
                        try {{
                            Object.defineProperty(media, 'enumerateDevices', {{
                                value: jubraEnumerateDevices,
                                configurable: true
                            }});
                        }} catch (e) {{
                            try {{ media.enumerateDevices = jubraEnumerateDevices; }} catch (ignored) {{}}
                        }}
                    }}

                    if (window.MediaDevices && MediaDevices.prototype && !__jubraKit.seen(MediaDevices.prototype)) {{
                        __jubraKit.markSeen(MediaDevices.prototype);
                        try {{
                            Object.defineProperty(MediaDevices.prototype, 'enumerateDevices', {{
                                value: jubraEnumerateDevices,
                                configurable: true
                            }});
                        }} catch (e) {{}}
                    }}
                }} catch (e) {{}}
            }};

            try {{
                installJubraMediaDevicesPatch();
                Promise.resolve().then(installJubraMediaDevicesPatch);
                setTimeout(installJubraMediaDevicesPatch, 0);
                setTimeout(installJubraMediaDevicesPatch, 500);
            }} catch (e) {{}}

            try {{
                const shouldControlWebRTC = ['disabled', 'disable', 'block', 'blocked', 'off', 'altered', 'controlled'].includes(__jubraWebRTCMode);
                const NativeRTCPeerConnection = window.RTCPeerConnection || window.webkitRTCPeerConnection;

                // Keep RTCPeerConnection constructable for a more natural surface, while Chrome flags
                // restrict non-proxied UDP and this JS layer sanitizes observable ICE/SDP text.
                const sanitizeWebRTCText = (value) => String(value || '')
                    .replace(/(\\\\d{{1,3}}\\\\.){{3}}\\\\d{{1,3}}/g, '0.0.0.0')
                    .replace(/([a-fA-F0-9]{{0,4}}:){{2,7}}[a-fA-F0-9]{{0,4}}/g, '::');

                const patchGetterText = (prototype, propertyName) => {{
                    try {{
                        if (!prototype) {{ return; }}
                        const descriptor = Object.getOwnPropertyDescriptor(prototype, propertyName);
                        if (descriptor && descriptor.get) {{
                            const nativeGet = descriptor.get;
                            const wrappedGetter = __jubraKit.tag(function() {{
                                return sanitizeWebRTCText(nativeGet.call(this));
                            }}, 'getter', propertyName);
                            Object.defineProperty(prototype, propertyName, {{
                                get: wrappedGetter,
                                configurable: true
                            }});
                        }}
                    }} catch (ignored) {{}}
                }};

                // ================================================================
                // PIXELSCAN FIX: Force RTCPeerConnection to use relay-only mode.
                //
                // Root cause: Chrome flag --force-webrtc-ip-handling-policy=
                // disable_non_proxied_udp blocks direct UDP AFTER the browser
                // starts, but the proxy auth extension has a timing gap -
                // WebRTC STUN requests can go out BEFORE proxy credentials
                // are configured, leaking the real IP (e.g. 24.98.240.24).
                //
                // JS-level sanitizeWebRTCText only scrubs text output; it
                // does NOT prevent actual network requests.
                //
                // Fix: Override the RTCPeerConnection constructor to always
                // inject iceTransportPolicy: 'relay' into the RTCConfiguration.
                // This forces ALL WebRTC traffic through a TURN relay server,
                // preventing any direct STUN/ICE requests that could leak
                // the user's real IP address. Also override setConfiguration()
                // to maintain relay-only policy even if the site tries to
                // change it.
                // ================================================================
                if (shouldControlWebRTC && NativeRTCPeerConnection) {{
                    const __OriginalRTCPeerConnection = NativeRTCPeerConnection;
                    const __relayConfig = (config) => {{
                        if (!config || typeof config !== 'object') {{
                            config = {{}};
                        }}
                        if (!Array.isArray(config.iceServers)) {{
                            config.iceServers = [];
                        }}
                        config.iceTransportPolicy = 'relay';
                        return config;
                    }};

                    const __JubraRTC = __jubraKit.tag(function(config, constraints) {{
                        return new __OriginalRTCPeerConnection(__relayConfig(config), constraints);
                    }}, 'method', 'RTCPeerConnection');
                    __JubraRTC.prototype = __OriginalRTCPeerConnection.prototype;
                    Object.setPrototypeOf(__JubraRTC, __OriginalRTCPeerConnection);

                    if (window.RTCPeerConnection) {{
                        window.RTCPeerConnection = __JubraRTC;
                    }}
                    if (window.webkitRTCPeerConnection) {{
                        window.webkitRTCPeerConnection = __JubraRTC;
                    }}

                    // Also override setConfiguration to maintain relay-only policy
                    if (__OriginalRTCPeerConnection.prototype.setConfiguration) {{
                        const __origSetConfig = __OriginalRTCPeerConnection.prototype.setConfiguration;
                        __OriginalRTCPeerConnection.prototype.setConfiguration = __jubraKit.tag(function(config) {{
                            return __origSetConfig.call(this, __relayConfig(config));
                        }}, 'method', 'setConfiguration');
                    }}
                }}

                if (NativeRTCPeerConnection && NativeRTCPeerConnection.prototype && !__jubraKit.seen(NativeRTCPeerConnection.prototype)) {{
                    __jubraKit.markSeen(NativeRTCPeerConnection.prototype);
                    const NativeRTCIceCandidate = window.RTCIceCandidate;
                    if (NativeRTCIceCandidate && NativeRTCIceCandidate.prototype && !__jubraKit.seen(NativeRTCIceCandidate.prototype)) {{
                        __jubraKit.markSeen(NativeRTCIceCandidate.prototype);
                        patchGetterText(NativeRTCIceCandidate.prototype, 'candidate');
                        patchGetterText(NativeRTCIceCandidate.prototype, 'address');
                        patchGetterText(NativeRTCIceCandidate.prototype, 'relatedAddress');
                    }}

                    const NativeRTCSessionDescription = window.RTCSessionDescription;
                    if (NativeRTCSessionDescription && NativeRTCSessionDescription.prototype && !__jubraKit.seen(NativeRTCSessionDescription.prototype)) {{
                        __jubraKit.markSeen(NativeRTCSessionDescription.prototype);
                        patchGetterText(NativeRTCSessionDescription.prototype, 'sdp');
                    }}

                    if (shouldControlWebRTC) {{
                        const controlledFns = new WeakSet();
                        try {{
                            const originalCreateOffer = NativeRTCPeerConnection.prototype.createOffer;
                            if (typeof originalCreateOffer === 'function' && !controlledFns.has(originalCreateOffer)) {{
                                const patchedCreateOffer = __jubraKit.tag(function(...args) {{
                                    return Promise.resolve(originalCreateOffer.apply(this, args)).then((description) => {{
                                        try {{
                                            if (description && typeof description.sdp === 'string') {{
                                                const cleanSdp = sanitizeWebRTCText(description.sdp);
                                                if (cleanSdp !== description.sdp) {{
                                                    return new RTCSessionDescription({{ type: description.type, sdp: cleanSdp }});
                                                }}
                                            }}
                                        }} catch (ignored) {{}}
                                        return description;
                                    }});
                                }}, 'method', 'createOffer');
                                controlledFns.add(patchedCreateOffer);
                                Object.defineProperty(NativeRTCPeerConnection.prototype, 'createOffer', {{ value: patchedCreateOffer, configurable: true }});
                            }}
                        }} catch (ignored) {{}}

                        try {{
                            const originalCreateAnswer = NativeRTCPeerConnection.prototype.createAnswer;
                            if (typeof originalCreateAnswer === 'function' && !controlledFns.has(originalCreateAnswer)) {{
                                const patchedCreateAnswer = __jubraKit.tag(function(...args) {{
                                    return Promise.resolve(originalCreateAnswer.apply(this, args)).then((description) => {{
                                        try {{
                                            if (description && typeof description.sdp === 'string') {{
                                                const cleanSdp = sanitizeWebRTCText(description.sdp);
                                                if (cleanSdp !== description.sdp) {{
                                                    return new RTCSessionDescription({{ type: description.type, sdp: cleanSdp }});
                                                }}
                                            }}
                                        }} catch (ignored) {{}}
                                        return description;
                                    }});
                                }}, 'method', 'createAnswer');
                                controlledFns.add(patchedCreateAnswer);
                                Object.defineProperty(NativeRTCPeerConnection.prototype, 'createAnswer', {{ value: patchedCreateAnswer, configurable: true }});
                            }}
                        }} catch (ignored) {{}}
                    }}
                }}
            }} catch (e) {{}}
            try {{
                if (document.fonts && !__jubraKit.seen(document.fonts)) {{
                    __jubraKit.markSeen(document.fonts);
                    const fontSet = new Set(__jubraFonts.map(font => String(font).toLowerCase()));

                    const extractFontFamily = (input) => {{
                        const text = String(input || '').replace(/["']/g, ' ');
                        for (const font of __jubraFonts) {{
                            if (text.toLowerCase().includes(String(font).toLowerCase())) {{
                                return String(font).toLowerCase();
                            }}
                        }}
                        const pieces = text.split(/[, ]+/).map(v => v.trim().toLowerCase()).filter(Boolean);
                        return pieces.length ? pieces[pieces.length - 1] : '';
                    }};

                    document.fonts.check = __jubraKit.tag(function(font, text) {{
                        const family = extractFontFamily(font);
                        if (family && fontSet.has(family)) {{
                            return true;
                        }}
                        // Keep the runtime font surface aligned to the saved OS font profile.
                        // Falling back to the native check leaks host/browser fallback fonts such as
                        // Menlo, Monaco, Apple Color Emoji, Ubuntu, Cantarell, etc. into Windows profiles.
                        return false;
                    }}, 'method', 'check');

                    document.fonts.load = __jubraKit.tag(function(font, text) {{
                        const family = extractFontFamily(font);
                        if (family && fontSet.has(family)) {{
                            return Promise.resolve([]);
                        }}
                        // Do not expose non-profile font families through FontFaceSet.load().
                        return Promise.resolve([]);
                    }}, 'method', 'load');
                }}
            }} catch (e) {{}}
        }})();
        """)

        color_depth = fingerprint.get('color_depth', 24)
        script_lines.append(f"""
        // Screen color depth override only.
        // Do not override window.devicePixelRatio; it can break page layout/zoom consistency.
        (() => {{
            __jubraKit.defineNativeGetter(Screen.prototype, 'colorDepth', {color_depth});
            __jubraKit.defineNativeGetter(Screen.prototype, 'pixelDepth', {color_depth});
        }})();
        """)

        touch_points = fingerprint.get('touch_points', 0)
        script_lines.append(f"""
        // Touch support
        if ({touch_points} > 0) {{
            (() => {{
                __jubraKit.defineNativeGetter(Navigator.prototype, 'maxTouchPoints', {touch_points});
            }})();
        }}
        """)

        # ================================================================
        # PIXELSCAN FIX: Intl.DateTimeFormat shim COMPLETELY REMOVED.
        #
        # Root cause: PixelScan detects "Timezone spoofed" by checking:
        #   1) Object.getOwnPropertyDescriptor(Intl, 'DateTimeFormat')
        #      shows a non-native descriptor (getter instead of value)
        #   2) Cross-realm inconsistency: main frame has the shim but
        #      Web Workers don't (Page.addScriptToEvaluateOnNewDocument
        #      does NOT inject into Worker scopes)
        #   3) Function.prototype.toString.call(Intl.DateTimeFormat)
        #      returns JS source instead of [native code]
        #
        # Fix: CDP Emulation.setTimezoneOverride is the ONLY timezone
        # method. It works at the V8 engine level, is completely
        # invisible to JavaScript detection, automatically applies to
        # Workers, iframes, and all new contexts. No JS shim needed.
        # ================================================================
        # (CDP timezone is already applied by _set_timezone_via_cdp and
        #  _start_cdp_timezone_controller in _launch_sync above)

        # ================================================================
        # FIX #12: Battery API blocking
        # Prevent navigator.getBattery() from exposing battery status
        # which can be used for fingerprinting.
        # ================================================================
        script_lines.append("""
        // Battery API blocking
        (() => {
            if (navigator.getBattery) {
                __jubraKit.defineNativeGetter(navigator, 'getBattery', undefined);
            }
        })();
        """)

        # ================================================================
        # FIX #12: Speech Synthesis API blocking
        # Prevent window.speechSynthesis.getVoices() from exposing
        # system voice list which creates a high-entropy fingerprint.
        # ================================================================
        script_lines.append("""
        // Speech Synthesis API - return minimal voice list
        (() => {
            if (window.speechSynthesis) {
                const __safeVoices = Object.freeze([
                    Object.create(null, {
                        voiceURI: { value: 'Microsoft David - English (United States)', enumerable: true },
                        name: { value: 'Microsoft David - English (United States)', enumerable: true },
                        lang: { value: 'en-US', enumerable: true },
                        localService: { value: true, enumerable: true },
                        default: { value: true, enumerable: true },
                    }),
                ]);
                const origGetVoices = window.speechSynthesis.getVoices;
                __jubraKit.defineNativeGetter(window.speechSynthesis, 'getVoices', __jubraKit.tag(function() { return __safeVoices; }, 'method', 'getVoices'));
            }
        })();
        """)

        # ================================================================
        # FIX #16: CSS Media Queries override (matchMedia)
        # Prevent prefers-color-scheme, prefers-reduced-motion, and
        # other media queries from leaking OS-level user preferences.
        # ================================================================
        script_lines.append("""
        // CSS matchMedia overrides for privacy
        (() => {
            const origMatchMedia = window.matchMedia;
            if (!origMatchMedia || __jubraKit.seen(window.matchMedia)) { return; }
            // We do NOT replace matchMedia entirely (that breaks CSS).
            // Instead, ensure specific sensitive queries return consistent values.
            const __safeMediaDefaults = {
                'prefers-color-scheme': 'light',
                'prefers-reduced-motion': 'no-preference',
                'prefers-contrast': 'no-preference',
                'prefers-reduced-transparency': 'no-preference',
                'forced-colors': 'none',
                'prefers-reduced-data': 'no-preference',
            };
            // Nothing further needed: matchMedia returns a MediaQueryList
            // whose .matches property is derived from the CSS media query.
            // These values are controlled by Chrome flags and OS settings.
            // For a privacy browser, the key is NOT to override matchMedia
            // (which is detectable), but to ensure the profile's OS/browser
            // settings produce consistent results. The Chrome flags
            // --force-color-profile=srgb already normalize color output.
        })();
        """)

        # ================================================================
        # FIX #13: Media device labels - hide identifying info
        # Empty labels prevent device-specific fingerprinting.
        # Labels are only visible after user grants permission anyway.
        # ================================================================
        # (Media device label handling is already in the main media devices
        #  block above where __jubraMediaDevices are defined. The labels
        #  there are OS-specific generic names like "Microphone Array"
        #  which are common enough to not be unique. For extra safety,
        #  the _media_devices_for_fingerprint method could return empty
        #  labels, but that would make the browser look unusual since
        #  real browsers DO show labels after permission grant.)

        return "\n".join(script_lines)
