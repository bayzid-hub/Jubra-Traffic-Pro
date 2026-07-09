"""
Jubra Traffic Pro - Config Panel
Full GUI-based configuration editor.
Users can configure everything from the app.

PATCH:
- Add async_loop to ConfigPanel so GUI can schedule async work on shared background loop.
- Replace _reload_proxies_sync(): remove threading + asyncio.new_event_loop + run_until_complete.
  Use asyncio.run_coroutine_threadsafe(..., async_loop) instead.
- UI feedback is kept minimal and thread-safe (uses Qt singleShot).

To install:
- Save this file as gui/config_panel.py in your project.
- Ensure gui/main_window.py constructs ConfigPanel(..., async_loop=<shared_loop>).
"""

import logging
import asyncio
from typing import Any, Dict, List, Optional
from pathlib import Path

try:
    from PyQt6.QtWidgets import (
        QWidget,
        QVBoxLayout,
        QHBoxLayout,
        QLabel,
        QLineEdit,
        QSpinBox,
        QDoubleSpinBox,
        QCheckBox,
        QPushButton,
        QTextEdit,
        QGroupBox,
        QFormLayout,
        QMessageBox,
        QComboBox,
        QTabWidget,
        QFileDialog,
        QScrollArea,
        QFrame,
    )
    from PyQt6.QtCore import Qt, QTimer
    HAS_QT = True
except ImportError:
    HAS_QT = False

logger = logging.getLogger(__name__)


class ConfigPanel(QWidget if HAS_QT else object):
    """
    Complete GUI Configuration Panel.

    Users can:
    - Add/Remove target URLs
    - Paste proxy list
    - Configure traffic settings
    - Adjust behavior parameters
    - Set analytics IDs
    - Save all changes with one click
    """

    def __init__(self, components: Dict[str, Any], async_loop: Any = None):
        if HAS_QT:
            super().__init__()
        self._components = components
        self._config     = components.get("config")
        self._widgets:   Dict[str, Any] = {}

        # PATCH: shared background loop (created in main.py and passed via MainWindow)
        self._async_loop = async_loop

        if HAS_QT:
            self._setup_ui()
            self._load_current_config()

    def _setup_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(12, 12, 12, 12)
        main_layout.setSpacing(10)

        header = QLabel("⚙️  Configuration Settings")
        header.setStyleSheet(
            "font-size: 20px; font-weight: bold; "
            "color: #e94560; padding: 5px;"
        )
        main_layout.addWidget(header)

        info = QLabel(
            "💡 Configure everything here. "
            "Click 'Save & Apply' at the bottom to save changes."
        )
        info.setStyleSheet(
            "color: #9e9e9e; font-size: 12px; padding: 4px;"
        )
        main_layout.addWidget(info)

        tabs = QTabWidget()
        tabs.setStyleSheet("""
            QTabBar::tab {
                padding: 8px 20px;
                font-weight: bold;
            }
        """)

        tabs.addTab(self._build_targets_tab(),  "🎯 Targets")
        tabs.addTab(self._build_proxy_tab(),    "🌐 Proxies")
        tabs.addTab(self._build_traffic_tab(),  "🚦 Traffic")
        tabs.addTab(self._build_browser_tab(),  "🌍 Browser")
        tabs.addTab(self._build_behavior_tab(), "🤖 Behavior")
        tabs.addTab(self._build_analytics_tab(), "📊 Analytics")

        main_layout.addWidget(tabs, 1)

        btn_layout = QHBoxLayout()

        save_btn = QPushButton("💾 Save & Apply")
        save_btn.setStyleSheet("""
            QPushButton {
                background-color: #4caf50;
                border-color: #4caf50;
                font-size: 14px;
                padding: 10px 30px;
            }
            QPushButton:hover {
                background-color: #388e3c;
            }
        """)
        save_btn.clicked.connect(self._save_config)

        reload_btn = QPushButton("🔄 Reload from File")
        reload_btn.clicked.connect(self._reload_config)

        reset_btn = QPushButton("↩️  Reset Defaults")
        reset_btn.clicked.connect(self._reset_defaults)

        btn_layout.addWidget(save_btn)
        btn_layout.addWidget(reload_btn)
        btn_layout.addWidget(reset_btn)
        btn_layout.addStretch()

        main_layout.addLayout(btn_layout)

    def _build_targets_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(16, 16, 16, 16)

        label = QLabel(
            "Enter target URLs (one per line):"
        )
        label.setStyleSheet("font-size: 13px; font-weight: bold;")
        layout.addWidget(label)

        hint = QLabel(
            "Example: https://yoursite.com"
        )
        hint.setStyleSheet(
            "color: #9e9e9e; font-size: 11px;"
        )
        layout.addWidget(hint)

        self._widgets["target_urls"] = QTextEdit()
        self._widgets["target_urls"].setPlaceholderText(
            "https://example.com\n"
            "https://example.com/page1\n"
            "https://example.com/page2"
        )
        self._widgets["target_urls"].setStyleSheet("""
            QTextEdit {
                background-color: #16213e;
                color: #e0e0e0;
                border: 1px solid #2d2d44;
                border-radius: 4px;
                padding: 8px;
                font-family: 'Consolas', monospace;
                font-size: 12px;
            }
        """)
        layout.addWidget(self._widgets["target_urls"], 1)

        return widget

    def _build_proxy_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(16, 16, 16, 16)

        top_layout = QHBoxLayout()

        label = QLabel(
            "Proxy List (one per line):"
        )
        label.setStyleSheet("font-size: 13px; font-weight: bold;")
        top_layout.addWidget(label)
        top_layout.addStretch()

        import_btn = QPushButton("📁 Import from File")
        import_btn.clicked.connect(self._import_proxies)
        top_layout.addWidget(import_btn)

        layout.addLayout(top_layout)

        hint = QLabel(
            "Supported formats:\n"
            "• host:port\n"
            "• host:port:user:pass\n"
            "• http://user:pass@host:port\n"
            "• socks5://host:port"
        )
        hint.setStyleSheet(
            "color: #9e9e9e; font-size: 11px; padding: 4px;"
        )
        layout.addWidget(hint)

        self._widgets["proxy_list"] = QTextEdit()
        self._widgets["proxy_list"].setPlaceholderText(
            "192.168.1.1:8080\n"
            "192.168.1.2:8080:username:password\n"
            "http://user:pass@proxy.com:8080\n"
            "socks5://proxy.com:1080"
        )
        self._widgets["proxy_list"].setStyleSheet("""
            QTextEdit {
                background-color: #16213e;
                color: #e0e0e0;
                border: 1px solid #2d2d44;
                border-radius: 4px;
                padding: 8px;
                font-family: 'Consolas', monospace;
                font-size: 12px;
            }
        """)
        layout.addWidget(self._widgets["proxy_list"], 1)

        options_layout = QHBoxLayout()

        self._widgets["proxy_enabled"] = QCheckBox("Enable Proxies")
        self._widgets["proxy_enabled"].setChecked(True)
        options_layout.addWidget(self._widgets["proxy_enabled"])

        options_layout.addWidget(QLabel("Rotation:"))
        self._widgets["rotation_strategy"] = QComboBox()
        self._widgets["rotation_strategy"].addItems([
            "weighted", "round_robin", "random",
            "least_used", "performance", "sticky",
        ])
        self._widgets["rotation_strategy"].setStyleSheet(
            self._input_style()
        )
        options_layout.addWidget(
            self._widgets["rotation_strategy"]
        )

        options_layout.addStretch()
        layout.addLayout(options_layout)

        return widget

    # --- The rest of the tab builders and helpers are unchanged from your file ---
    # For brevity in this patch artifact, we keep the original implementations.

    def _build_traffic_tab(self) -> QWidget:
        # (UNCHANGED) -- keep your existing implementation
        widget = QWidget()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("background: transparent; border: none;")

        inner  = QWidget()
        form   = QFormLayout(inner)
        form.setSpacing(12)
        form.setContentsMargins(16, 16, 16, 16)

        volume_group = QGroupBox("📊 Volume Settings")
        volume_group.setStyleSheet(self._group_style())
        volume_layout = QFormLayout(volume_group)

        self._widgets["total_sessions"] = self._spin(1, 100000, 100)
        volume_layout.addRow(
            "Total Sessions:", self._widgets["total_sessions"]
        )

        self._widgets["sessions_per_hour"] = self._spin(1, 1000, 30)
        volume_layout.addRow(
            "Sessions per Hour:", self._widgets["sessions_per_hour"]
        )

        self._widgets["daily_limit"] = self._spin(0, 100000, 0)
        volume_layout.addRow(
            "Daily Limit (0=unlimited):",
            self._widgets["daily_limit"]
        )

        form.addRow(volume_group)

        mix_group = QGroupBox("🚦 Traffic Mix (must total 1.0)")
        mix_group.setStyleSheet(self._group_style())
        mix_layout = QFormLayout(mix_group)

        self._widgets["organic_ratio"] = self._double_spin(0.0, 1.0, 0.60)
        mix_layout.addRow(
            "Organic (Search):", self._widgets["organic_ratio"]
        )

        self._widgets["social_ratio"] = self._double_spin(0.0, 1.0, 0.15)
        mix_layout.addRow(
            "Social Media:", self._widgets["social_ratio"]
        )

        self._widgets["direct_ratio"] = self._double_spin(0.0, 1.0, 0.15)
        mix_layout.addRow(
            "Direct:", self._widgets["direct_ratio"]
        )

        self._widgets["referral_ratio"] = self._double_spin(0.0, 1.0, 0.10)
        mix_layout.addRow(
            "Referral:", self._widgets["referral_ratio"]
        )

        form.addRow(mix_group)

        device_group = QGroupBox("📱 Device Distribution")
        device_group.setStyleSheet(self._group_style())
        device_layout = QFormLayout(device_group)

        self._widgets["desktop_ratio"] = self._double_spin(0.0, 1.0, 0.65)
        device_layout.addRow(
            "Desktop:", self._widgets["desktop_ratio"]
        )

        self._widgets["mobile_ratio"] = self._double_spin(0.0, 1.0, 0.30)
        device_layout.addRow(
            "Mobile:", self._widgets["mobile_ratio"]
        )

        self._widgets["tablet_ratio"] = self._double_spin(0.0, 1.0, 0.05)
        device_layout.addRow(
            "Tablet:", self._widgets["tablet_ratio"]
        )

        form.addRow(device_group)

        session_group = QGroupBox("⏱️  Session Duration")
        session_group.setStyleSheet(self._group_style())
        session_layout = QFormLayout(session_group)

        self._widgets["min_session_duration"] = self._spin(5, 3600, 45)
        session_layout.addRow(
            "Min Duration (seconds):",
            self._widgets["min_session_duration"]
        )

        self._widgets["max_session_duration"] = self._spin(10, 7200, 480)
        session_layout.addRow(
            "Max Duration (seconds):",
            self._widgets["max_session_duration"]
        )

        self._widgets["bounce_rate"] = self._double_spin(0.0, 1.0, 0.35)
        session_layout.addRow(
            "Bounce Rate:", self._widgets["bounce_rate"]
        )

        form.addRow(session_group)

        scroll.setWidget(inner)

        outer = QVBoxLayout(widget)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(scroll)

        return widget

    def _build_browser_tab(self) -> QWidget:
        # (UNCHANGED)
        widget = QWidget()
        form   = QFormLayout(widget)
        form.setSpacing(12)
        form.setContentsMargins(16, 16, 16, 16)

        self._widgets["pool_size"] = self._spin(1, 50, 5)
        form.addRow(
            "Browser Pool Size:", self._widgets["pool_size"]
        )

        self._widgets["headless"] = QCheckBox("Headless Mode")
        self._widgets["headless"].setChecked(True)
        form.addRow("", self._widgets["headless"])

        self._widgets["page_load_timeout"] = self._spin(5, 120, 30)
        form.addRow(
            "Page Load Timeout (s):",
            self._widgets["page_load_timeout"]
        )

        self._widgets["recycle_after"] = self._spin(1, 500, 50)
        form.addRow(
            "Recycle After N Pages:",
            self._widgets["recycle_after"]
        )

        self._widgets["crash_recovery"] = QCheckBox(
            "Auto Crash Recovery"
        )
        self._widgets["crash_recovery"].setChecked(True)
        form.addRow("", self._widgets["crash_recovery"])

        self._widgets["disable_images"] = QCheckBox(
            "Disable Images (faster)"
        )
        form.addRow("", self._widgets["disable_images"])

        self._widgets["warmup_count"] = self._spin(0, 20, 2)
        form.addRow(
            "Pre-warm Browsers:", self._widgets["warmup_count"]
        )

        return widget

    def _build_behavior_tab(self) -> QWidget:
        # (UNCHANGED)
        widget = QWidget()
        form   = QFormLayout(widget)
        form.setSpacing(12)
        form.setContentsMargins(16, 16, 16, 16)

        self._widgets["mouse_enabled"] = QCheckBox(
            "Enable Mouse Simulation"
        )
        self._widgets["mouse_enabled"].setChecked(True)
        form.addRow("", self._widgets["mouse_enabled"])

        self._widgets["scroll_enabled"] = QCheckBox(
            "Enable Scroll Simulation"
        )
        self._widgets["scroll_enabled"].setChecked(True)
        form.addRow("", self._widgets["scroll_enabled"])

        self._widgets["keyboard_enabled"] = QCheckBox(
            "Enable Keyboard Simulation"
        )
        self._widgets["keyboard_enabled"].setChecked(True)
        form.addRow("", self._widgets["keyboard_enabled"])

        self._widgets["typing_wpm_min"] = self._spin(10, 200, 35)
        form.addRow(
            "Min Typing WPM:", self._widgets["typing_wpm_min"]
        )

        self._widgets["typing_wpm_max"] = self._spin(10, 300, 95)
        form.addRow(
            "Max Typing WPM:", self._widgets["typing_wpm_max"]
        )

        self._widgets["scroll_speed"] = QComboBox()
        self._widgets["scroll_speed"].addItems([
            "slow", "normal", "fast", "random"
        ])
        self._widgets["scroll_speed"].setCurrentText("random")
        self._widgets["scroll_speed"].setStyleSheet(
            self._input_style()
        )
        form.addRow(
            "Scroll Speed:", self._widgets["scroll_speed"]
        )

        self._widgets["idle_probability"] = self._double_spin(
            0.0, 1.0, 0.08
        )
        form.addRow(
            "Idle Probability:", self._widgets["idle_probability"]
        )

        self._widgets["attention_model"] = QCheckBox(
            "Enable Attention Model"
        )
        self._widgets["attention_model"].setChecked(True)
        form.addRow("", self._widgets["attention_model"])

        return widget

    def _build_analytics_tab(self) -> QWidget:
        # (UNCHANGED)
        widget = QWidget()
        form   = QFormLayout(widget)
        form.setSpacing(12)
        form.setContentsMargins(16, 16, 16, 16)
        
        ga4_group = QGroupBox("📈 Google Analytics 4")
        ga4_group.setStyleSheet(self._group_style())
        ga4_layout = QFormLayout(ga4_group)

        self._widgets["ga4_enabled"] = QCheckBox("Enable GA4")
        ga4_layout.addRow("", self._widgets["ga4_enabled"])

        self._widgets["ga4_measurement_id"] = QLineEdit()
        self._widgets["ga4_measurement_id"].setPlaceholderText(
            "G-XXXXXXXXXX"
        )
        self._widgets["ga4_measurement_id"].setStyleSheet(
            self._input_style()
        )
        ga4_layout.addRow(
            "Measurement ID:",
            self._widgets["ga4_measurement_id"]
        )

        self._widgets["ga4_api_secret"] = QLineEdit()
        self._widgets["ga4_api_secret"].setEchoMode(
            QLineEdit.EchoMode.Password
        )
        self._widgets["ga4_api_secret"].setStyleSheet(
            self._input_style()
        )
        ga4_layout.addRow(
            "API Secret:", self._widgets["ga4_api_secret"]
        )

        form.addRow(ga4_group)

        pixel_group = QGroupBox("📘 Facebook/Meta Pixel")
        pixel_group.setStyleSheet(self._group_style())
        pixel_layout = QFormLayout(pixel_group)

        self._widgets["pixel_enabled"] = QCheckBox("Enable Pixel")
        pixel_layout.addRow("", self._widgets["pixel_enabled"])

        self._widgets["pixel_id"] = QLineEdit()
        self._widgets["pixel_id"].setPlaceholderText(
            "123456789012345"
        )
        self._widgets["pixel_id"].setStyleSheet(
            self._input_style()
        )
        pixel_layout.addRow(
            "Pixel ID:", self._widgets["pixel_id"]
        )

        form.addRow(pixel_group)

        captcha_group = QGroupBox("🔐 CAPTCHA Solver")
        captcha_group.setStyleSheet(self._group_style())
        captcha_layout = QFormLayout(captcha_group)

        self._widgets["captcha_service"] = QComboBox()
        self._widgets["captcha_service"].addItems([
            "none", "2captcha", "anticaptcha", "capmonster"
        ])
        self._widgets["captcha_service"].setStyleSheet(
            self._input_style()
        )
        captcha_layout.addRow(
            "Service:", self._widgets["captcha_service"]
        )

        self._widgets["captcha_api_key"] = QLineEdit()
        self._widgets["captcha_api_key"].setEchoMode(
            QLineEdit.EchoMode.Password
        )
        self._widgets["captcha_api_key"].setStyleSheet(
            self._input_style()
        )
        captcha_layout.addRow(
            "API Key:", self._widgets["captcha_api_key"]
        )

        self._widgets["captcha_budget"] = self._double_spin(
            0, 1000, 10.0
        )
        captcha_layout.addRow(
            "Budget (USD):", self._widgets["captcha_budget"]
        )

        form.addRow(captcha_group)

        return widget

    def _input_style(self) -> str:
        return """
            QComboBox, QLineEdit, QSpinBox, QDoubleSpinBox {
                background-color: #16213e;
                color: #e0e0e0;
                border: 1px solid #2d2d44;
                border-radius: 4px;
                padding: 4px 8px;
                min-width: 180px;
            }
            QComboBox::drop-down { border: none; }
            QComboBox QAbstractItemView {
                background-color: #16213e;
                color: #e0e0e0;
                selection-background-color: #0f3460;
            }
        """

    def _group_style(self) -> str:
        return """
            QGroupBox {
                color: #e0e0e0;
                border: 1px solid #2d2d44;
                border-radius: 4px;
                margin-top: 12px;
                padding-top: 16px;
                font-weight: bold;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 8px;
                color: #e94560;
            }
        """

    def _spin(self, min_v, max_v, default):
        w = QSpinBox()
        w.setRange(min_v, max_v)
        w.setValue(default)
        w.setStyleSheet(self._input_style())
        return w

    def _double_spin(self, min_v, max_v, default):
        w = QDoubleSpinBox()
        w.setRange(min_v, max_v)
        w.setValue(default)
        w.setSingleStep(0.05)
        w.setDecimals(2)
        w.setStyleSheet(self._input_style())
        return w

    def _load_current_config(self):
        if not self._config:
            return

        try:
            urls = self._config.get("traffic.target_urls", [])
            if urls:
                self._widgets["target_urls"].setPlainText(
                    "\n".join(urls)
                )

            proxy_file = self._config.get(
                "proxy.pool_file", "data/proxies.txt"
            )
            proxy_path = Path(proxy_file)
            if proxy_path.exists():
                content = proxy_path.read_text(encoding="utf-8")
                self._widgets["proxy_list"].setPlainText(content)

            settings_map = {
                "proxy_enabled":        "proxy.enabled",
                "rotation_strategy":    "proxy.rotation_strategy",
                "total_sessions":       "traffic.total_sessions",
                "sessions_per_hour":    "traffic.sessions_per_hour",
                "daily_limit":          "traffic.daily_limit",
                "organic_ratio":        "traffic.organic_ratio",
                "social_ratio":         "traffic.social_ratio",
                "direct_ratio":         "traffic.direct_ratio",
                "referral_ratio":       "traffic.referral_ratio",
                "bounce_rate":          "traffic.bounce_rate",
                "min_session_duration": "traffic.min_session_duration",
                "max_session_duration": "traffic.max_session_duration",
                "pool_size":            "browser.pool_size",
                "headless":             "browser.headless",
                "page_load_timeout":    "browser.page_load_timeout",
                "recycle_after":        "browser.recycle_after",
                "crash_recovery":       "browser.crash_recovery",
                "disable_images":       "browser.disable_images",
                "warmup_count":         "browser.warmup_count",
                "mouse_enabled":        "behavior.mouse_enabled",
                "scroll_enabled":       "behavior.scroll_enabled",
                "keyboard_enabled":     "behavior.keyboard_enabled",
                "typing_wpm_min":       "behavior.typing_wpm_min",
                "typing_wpm_max":       "behavior.typing_wpm_max",
                "scroll_speed":         "behavior.scroll_speed",
                "idle_probability":     "behavior.idle_probability",
                "ga4_enabled":          "analytics.ga4_enabled",
                "ga4_measurement_id":   "analytics.ga4_measurement_id",
                "ga4_api_secret":       "analytics.ga4_api_secret",
                "pixel_enabled":        "analytics.pixel_enabled",
                "pixel_id":             "analytics.pixel_id",
                "captcha_service":      "security.captcha_service",
                "captcha_api_key":      "security.captcha_api_key",
                "captcha_budget":       "security.captcha_budget",
            }

            for widget_key, config_key in settings_map.items():
                widget = self._widgets.get(widget_key)
                if not widget:
                    continue
                value = self._config.get(config_key)
                if value is None:
                    continue

                if isinstance(widget, QCheckBox):
                    widget.setChecked(bool(value))
                elif isinstance(widget, QSpinBox):
                    widget.setValue(int(value))
                elif isinstance(widget, QDoubleSpinBox):
                    widget.setValue(float(value))
                elif isinstance(widget, QLineEdit):
                    widget.setText(str(value))
                elif isinstance(widget, QComboBox):
                    idx = widget.findText(str(value))
                    if idx >= 0:
                        widget.setCurrentIndex(idx)

        except Exception as exc:
            logger.error(
                f"[ConfigPanel] Load error: {exc}"
            )

    def _save_config(self):
        if not self._config:
            QMessageBox.warning(
                self, "Error", "Config manager not available"
            )
            return

        try:
            urls_text = self._widgets["target_urls"].toPlainText()
            urls = [
                u.strip() for u in urls_text.splitlines()
                if u.strip() and not u.startswith("#")
            ]
            self._config.set("traffic.target_urls", urls)

            proxy_text = self._widgets["proxy_list"].toPlainText()
            proxy_file = self._config.get(
                "proxy.pool_file", "data/proxies.txt"
            )
            proxy_path = Path(proxy_file)
            proxy_path.parent.mkdir(parents=True, exist_ok=True)
            proxy_path.write_text(proxy_text, encoding="utf-8")

            settings_map = {
                "proxy_enabled":        "proxy.enabled",
                "rotation_strategy":    "proxy.rotation_strategy",
                "total_sessions":       "traffic.total_sessions",
                "sessions_per_hour":    "traffic.sessions_per_hour",
                "daily_limit":          "traffic.daily_limit",
                "organic_ratio":        "traffic.organic_ratio",
                "social_ratio":         "traffic.social_ratio",
                "direct_ratio":         "traffic.direct_ratio",
                "referral_ratio":       "traffic.referral_ratio",
                "bounce_rate":          "traffic.bounce_rate",
                "min_session_duration": "traffic.min_session_duration",
                "max_session_duration": "traffic.max_session_duration",
                "pool_size":            "browser.pool_size",
                "headless":             "browser.headless",
                "page_load_timeout":    "browser.page_load_timeout",
                "recycle_after":        "browser.recycle_after",
                "crash_recovery":       "browser.crash_recovery",
                "disable_images":       "browser.disable_images",
                "warmup_count":         "browser.warmup_count",
                "mouse_enabled":        "behavior.mouse_enabled",
                "scroll_enabled":       "behavior.scroll_enabled",
                "keyboard_enabled":     "behavior.keyboard_enabled",
                "typing_wpm_min":       "behavior.typing_wpm_min",
                "typing_wpm_max":       "behavior.typing_wpm_max",
                "scroll_speed":         "behavior.scroll_speed",
                "idle_probability":     "behavior.idle_probability",
                "ga4_enabled":          "analytics.ga4_enabled",
                "ga4_measurement_id":   "analytics.ga4_measurement_id",
                "ga4_api_secret":       "analytics.ga4_api_secret",
                "pixel_enabled":        "analytics.pixel_enabled",
                "pixel_id":             "analytics.pixel_id",
                "captcha_service":      "security.captcha_service",
                "captcha_api_key":      "security.captcha_api_key",
                "captcha_budget":       "security.captcha_budget",
            }

            for widget_key, config_key in settings_map.items():
                widget = self._widgets.get(widget_key)
                if not widget:
                    continue

                if isinstance(widget, QCheckBox):
                    value = widget.isChecked()
                elif isinstance(widget, (QSpinBox, QDoubleSpinBox)):
                    value = widget.value()
                elif isinstance(widget, QLineEdit):
                    value = widget.text().strip()
                elif isinstance(widget, QComboBox):
                    value = widget.currentText()
                else:
                    continue

                self._config.set(config_key, value)

            self._config.save()

            QMessageBox.information(
                self,
                "✅ Saved",
                f"Configuration saved successfully!\n\n"
                f"• {len(urls)} target URLs\n"
                f"• {len(proxy_text.splitlines())} proxies\n"
                f"• All settings applied",
            )

            logger.info(
                "[ConfigPanel] Config saved via GUI"
            )

            logger.info(
                "[ConfigPanel] Traffic config saved: total_sessions=%s, sessions_per_hour=%s",
                self._config.get("traffic.total_sessions"),
                self._config.get("traffic.sessions_per_hour"),
            )

            proxy_engine = self._components.get("proxy_engine")
            if proxy_engine and proxy_text.strip():
                self._reload_proxies_sync(
                    str(proxy_path), proxy_engine
                )


        except Exception as exc:
            QMessageBox.critical(
                self,
                "Error",
                f"Failed to save:\n\n{exc}"
            )
            logger.error(f"[ConfigPanel] Save error: {exc}")

    def _reload_config(self):
        if not self._config:
            return
        try:
            self._config.reload()
            self._load_current_config()
            QMessageBox.information(
                self, "🔄 Reloaded",
                "Config reloaded from file successfully!"
            )
        except Exception as exc:
            QMessageBox.critical(
                self, "Error", f"Reload failed:\n{exc}"
            )

    def _reset_defaults(self):
        reply = QMessageBox.question(
            self,
            "Reset Defaults",
            "Reset all settings to defaults?\n\n"
            "This will not affect saved target URLs or proxies.",
            QMessageBox.StandardButton.Yes |
            QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            QMessageBox.information(
                self, "Reset",
                "Defaults will be applied on next save."
            )

    def _import_proxies(self):
        filepath, _ = QFileDialog.getOpenFileName(
            self,
            "Import Proxy List",
            "",
            "Text Files (*.txt);;All Files (*.*)"
        )
        if filepath:
            try:
                content = Path(filepath).read_text(encoding="utf-8")
                self._widgets["proxy_list"].setPlainText(content)
                QMessageBox.information(
                    self, "✅ Imported",
                    f"Imported proxies from:\n{filepath}"
                )
            except Exception as exc:
                QMessageBox.critical(
                    self, "Error", f"Import failed:\n{exc}"
                )

    # ------------------------
    # PATCHED METHOD
    # ------------------------
    def _reload_proxies_sync(
        self,
        filepath: str,
        proxy_engine: Any,
    ) -> None:
        """Reload proxies without creating a new event loop or thread.

        Old behavior (buggy in GUI apps):
        - threading.Thread + asyncio.new_event_loop + loop.run_until_complete + loop.close

        New behavior:
        - schedule the async reload on shared background loop
        - update UI safely via Qt timer
        """
        if not self._async_loop:
            logger.warning(
                "[ConfigPanel] No async_loop provided; skipping proxy reload."
            )
            return

        async def do_reload():
            return await proxy_engine.load_from_file(
                filepath, validate=False
            )

        fut = asyncio.run_coroutine_threadsafe(
            do_reload(), self._async_loop
        )

        def _done_cb(f):
            try:
                count = f.result()
                logger.info(
                    f"[ConfigPanel] Loaded {count} proxies"
                )

                if HAS_QT:
                    def _ui():
                        try:
                            QMessageBox.information(
                                self,
                                "✅ Proxies Reloaded",
                                f"Loaded {count} proxies from:\n{filepath}",
                            )
                        except Exception:
                            # Avoid GUI hard-fail if panel is closing
                            pass
                    QTimer.singleShot(0, _ui)

            except Exception as exc:
                logger.error(
                    f"[ConfigPanel] Proxy load error: {exc}"
                )
                if HAS_QT:
                    def _ui_err():
                        try:
                            QMessageBox.warning(
                                self,
                                "Proxy Reload Failed",
                                f"Failed to reload proxies:\n{exc}",
                            )
                        except Exception:
                            pass
                    QTimer.singleShot(0, _ui_err)

        fut.add_done_callback(_done_cb)