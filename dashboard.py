""" Jubra Traffic Pro - Dashboard Real-time traffic overview with campaign control, metric tiles, and activity feed. """

import asyncio
import threading
import time
import logging
from typing import Any, Dict, List, Optional

try:
    from PyQt6.QtWidgets import (
        QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame, QScrollArea,
        QPushButton, QProgressBar, QGroupBox, QSizePolicy, QMessageBox,
    )
    from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QObject
    from PyQt6.QtGui import QFont, QColor
    HAS_QT = True
except ImportError:
    HAS_QT = False

logger = logging.getLogger(__name__)

class MetricTile(QWidget if HAS_QT else object):
    """A single metric display tile."""
    def __init__(
        self,
        title: str,
        value: str = "0",
        unit:  str = "",
        color: str = "#e94560",
    ):
        if HAS_QT:
            super().__init__()
            self._setup_ui(title, value, unit, color)

    def _setup_ui(self, title, value, unit, color):
        self.setFixedSize(190, 100)
        self.setStyleSheet(f"""
            QWidget {{
                background-color: #16213e;
                border: 2px solid {color};
                border-radius: 10px;
            }}
        """)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(4)
        self._title_label = QLabel(title)
        self._title_label.setStyleSheet(
            "color: #9e9e9e; font-size: 11px; "
            "border: none; font-weight: bold;"
        )
        self._value_label = QLabel(value)
        self._value_label.setStyleSheet(
            f"color: {color}; font-size: 28px; "
            f"font-weight: bold; border: none;"
        )
        self._unit_label = QLabel(unit)
        self._unit_label.setStyleSheet(
            "color: #666666; font-size: 10px; border: none;"
        )
        layout.addWidget(self._title_label)
        layout.addWidget(self._value_label)
        layout.addWidget(self._unit_label)

    def set_value(self, value: str):
        if HAS_QT and hasattr(self, "_value_label"):
            self._value_label.setText(str(value))

class CampaignCard(QFrame if HAS_QT else object):
    """Card displaying a campaign's status and controls."""
    def __init__(
        self,
        campaign_data: Dict[str, Any],
        components:    Dict[str, Any] = None,
        async_loop: Any = None,
    ):
        if HAS_QT:
            super().__init__()
        self._components = components or {}
        self._async_loop = async_loop
        self._campaign_id = campaign_data.get("campaign_id", "")
        if HAS_QT:
            self._setup_ui(campaign_data)

    def _setup_ui(self, data: Dict):
        self.setStyleSheet("""
            QFrame {
                background-color: #16213e;
                border: 1px solid #2d2d44;
                border-radius: 8px;
                padding: 4px;
            }
        """)
        self.setMinimumHeight(140)
        layout = QVBoxLayout(self)
        layout.setSpacing(6)
        
        header = QHBoxLayout()
        name_label = QLabel(data.get("name", "Campaign"))
        name_label.setStyleSheet(
            "font-weight: bold; font-size: 14px; "
            "color: #e0e0e0; border: none;"
        )
        status = data.get("status", "unknown")
        status_color = {
            "running":   "#4caf50",
            "paused":    "#ff9800",
            "completed": "#2196f3",
            "failed":    "#f44336",
            "pending":   "#9e9e9e",
            "cancelled": "#f44336",
        }.get(status, "#9e9e9e")
        status_label = QLabel(f"● {status.upper()}")
        status_label.setStyleSheet(
            f"color: {status_color}; font-size: 11px; "
            f"font-weight: bold; border: none;"
        )
        header.addWidget(name_label)
        header.addStretch()
        header.addWidget(status_label)
        
        progress_bar = QProgressBar()
        pct = int(data.get("completion_rate", 0) * 100)
        progress_bar.setValue(pct)
        progress_bar.setFormat(f"{pct}% Complete")
        progress_bar.setStyleSheet("""
            QProgressBar {
                background-color: #1a1a2e;
                border: 1px solid #2d2d44;
                border-radius: 4px;
                height: 18px;
                text-align: center;
                color: #ffffff;
                font-size: 11px;
                font-weight: bold;
            }
            QProgressBar::chunk {
                background-color: #e94560;
                border-radius: 3px;
            }
        """)
        
        stats = QHBoxLayout()
        launched  = data.get("sessions_launched",  0)
        total     = data.get("total_sessions",     0)
        success_r = data.get("success_rate",       0)
        stat_items = [
            f"📊 {launched}/{total} sessions",
            f"✅ {success_r * 100:.1f}% success",
            f"⚡ {data.get('sessions_per_hour', 0)}/hr",
        ]
        for text in stat_items:
            lbl = QLabel(text)
            lbl.setStyleSheet(
                "color: #9e9e9e; font-size: 11px; border: none;"
            )
            stats.addWidget(lbl)
            stats.addStretch()
            
        controls = QHBoxLayout()
        if status == "running":
            pause_btn = QPushButton("⏸️  Pause")
            pause_btn.setStyleSheet(self._btn_style("#ff9800"))
            pause_btn.clicked.connect(self._on_pause)
            controls.addWidget(pause_btn)
            stop_btn = QPushButton("⏹️  Stop")
            stop_btn.setStyleSheet(self._btn_style("#f44336"))
            stop_btn.clicked.connect(self._on_stop)
            controls.addWidget(stop_btn)
        elif status == "paused":
            resume_btn = QPushButton("▶️  Resume")
            resume_btn.setStyleSheet(self._btn_style("#4caf50"))
            resume_btn.clicked.connect(self._on_resume)
            controls.addWidget(resume_btn)
            stop_btn = QPushButton("⏹️  Stop")
            stop_btn.setStyleSheet(self._btn_style("#f44336"))
            stop_btn.clicked.connect(self._on_stop)
            controls.addWidget(stop_btn)
        elif status == "pending":
            start_btn = QPushButton("▶️  Start")
            start_btn.setStyleSheet(self._btn_style("#4caf50"))
            start_btn.clicked.connect(self._on_resume)
            controls.addWidget(start_btn)
        controls.addStretch()
        
        layout.addLayout(header)
        layout.addWidget(progress_bar)
        layout.addLayout(stats)
        layout.addLayout(controls)

    def _btn_style(self, color: str) -> str:
        return f"""
            QPushButton {{
                background-color: {color};
                color: white;
                border: none;
                border-radius: 4px;
                padding: 6px 14px;
                font-size: 11px;
                font-weight: bold;
                border: 1px solid {color};
            }}
            QPushButton:hover {{
                background-color: #ffffff;
                color: {color};
            }}
        """

    def _on_pause(self):
        orch = self._components.get("traffic_orchestrator")
        if orch and self._async_loop:
            asyncio.run_coroutine_threadsafe(
                orch.pause_campaign(self._campaign_id), self._async_loop
            )

    def _on_resume(self):
        orch = self._components.get("traffic_orchestrator")
        if orch and self._async_loop:
            asyncio.run_coroutine_threadsafe(
                orch.resume_campaign(self._campaign_id), self._async_loop
            )

    def _on_stop(self):
        orch = self._components.get("traffic_orchestrator")
        if orch and self._async_loop:
            asyncio.run_coroutine_threadsafe(
                orch.stop_campaign(self._campaign_id, "user_stop"), self._async_loop
            )

class Dashboard(QWidget if HAS_QT else object):
    """Real-Time Dashboard Tab."""
    def __init__(self, components: Dict[str, Any], async_loop: Any = None):
        if HAS_QT:
            super().__init__()
        self._components = components
        self._async_loop = async_loop
        self._last_activity_state = {
            "proxies": None,
            "verified": 0,
            "failed": 0,
            "launched": 0,
            "campaign_status": {},
        }
        if HAS_QT:
            self._setup_ui()

    def _setup_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(12, 12, 12, 12)
        main_layout.setSpacing(12)
        
        header_layout = QHBoxLayout()
        header = QLabel("📊 Real-Time Dashboard")
        header.setStyleSheet(
            "font-size: 20px; font-weight: bold; color: #e94560;"
        )
        header_layout.addWidget(header)
        header_layout.addStretch()
        
        self._start_btn = QPushButton("🚀  START CAMPAIGN")
        self._start_btn.setStyleSheet("""
            QPushButton {
                background-color: #4caf50;
                color: white;
                border: 2px solid #4caf50;
                border-radius: 6px;
                padding: 10px 24px;
                font-size: 13px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #388e3c;
                border-color: #388e3c;
            }
        """)
        self._start_btn.setMinimumHeight(40)
        self._start_btn.clicked.connect(self._on_start_campaign)
        header_layout.addWidget(self._start_btn)
        
        self._stop_all_btn = QPushButton("⏹️  STOP ALL")
        self._stop_all_btn.setStyleSheet("""
            QPushButton {
                background-color: #f44336;
                color: white;
                border: 2px solid #f44336;
                border-radius: 6px;
                padding: 10px 24px;
                font-size: 13px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #d32f2f;
                border-color: #d32f2f;
            }
        """)
        self._stop_all_btn.setMinimumHeight(40)
        self._stop_all_btn.clicked.connect(self._on_stop_all)
        header_layout.addWidget(self._stop_all_btn)
        
        main_layout.addLayout(header_layout)
        
        tiles_layout = QHBoxLayout()
        tiles_layout.setSpacing(12)
        self._tiles = {
            "sessions":     MetricTile(
                "Active Sessions", "0", "", "#e94560"
            ),
            "launched":     MetricTile(
                "Verified Loads", "0", "", "#2196f3"
            ),
            "success_rate": MetricTile(
                "Success Rate", "0%", "", "#4caf50"
            ),
            "proxies":      MetricTile(
                "Proxies Available", "0", "", "#ff9800"
            ),
            "detected":     MetricTile(
                "Bot Detected", "0", "", "#9c27b0"
            ),
            "sessions_hr":  MetricTile(
                "Sessions/Hour", "0", "/hr", "#00bcd4"
            ),
        }
        for tile in self._tiles.values():
            tiles_layout.addWidget(tile)
        tiles_layout.addStretch()
        
        tiles_frame = QFrame()
        tiles_frame.setLayout(tiles_layout)
        main_layout.addWidget(tiles_frame)
        
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("color: #2d2d44;")
        main_layout.addWidget(sep)
        
        content_layout = QHBoxLayout()
        campaigns_group = QGroupBox("📋 Campaigns")
        campaigns_group.setStyleSheet(self._group_style())
        campaigns_layout = QVBoxLayout(campaigns_group)
        
        self._campaign_scroll = QScrollArea()
        self._campaign_scroll.setWidgetResizable(True)
        self._campaign_scroll.setStyleSheet(
            "background: transparent; border: none;"
        )
        self._campaign_container = QWidget()
        self._campaign_inner = QVBoxLayout(self._campaign_container)
        self._campaign_inner.setSpacing(8)
        self._campaign_inner.addStretch()
        self._campaign_scroll.setWidget(self._campaign_container)
        campaigns_layout.addWidget(self._campaign_scroll)
        campaigns_group.setMinimumWidth(600)
        
        activity_group = QGroupBox("📢 Activity Feed")
        activity_group.setStyleSheet(self._group_style())
        activity_layout = QVBoxLayout(activity_group)
        
        self._activity_scroll = QScrollArea()
        self._activity_scroll.setWidgetResizable(True)
        self._activity_scroll.setStyleSheet(
            "background: transparent; border: none;"
        )
        self._activity_container = QWidget()
        self._activity_inner = QVBoxLayout(self._activity_container)
        self._activity_inner.setAlignment(Qt.AlignmentFlag.AlignTop)
        self._activity_scroll.setWidget(self._activity_container)
        activity_layout.addWidget(self._activity_scroll)
        
        content_layout.addWidget(campaigns_group, 2)
        content_layout.addWidget(activity_group, 1)
        main_layout.addLayout(content_layout, 1)
        
        self._empty_label = None
        self._show_empty_state()
        self.add_activity(
            "Dashboard ready. Click START CAMPAIGN to begin.",
            "info",
        )

    def _group_style(self) -> str:
        return """
            QGroupBox {
                color: #e0e0e0;
                border: 1px solid #2d2d44;
                border-radius: 6px;
                padding-top: 20px;
                font-weight: bold;
                font-size: 13px;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                color: #e94560;
                padding: 0 4px;
            }
        """

    def _show_empty_state(self):
        if self._empty_label is not None:
            return
        self._empty_label = QLabel(
            "No active campaigns.\n\n"
            "1. Go to Config tab\n"
            "2. Add Target URLs and Proxies\n"
            "3. Click Save & Apply\n"
            "4. Click 🚀 START CAMPAIGN above"
        )
        self._empty_label.setAlignment(
            Qt.AlignmentFlag.AlignCenter
        )
        self._empty_label.setStyleSheet(
            "color: #666666; font-size: 13px; padding: 40px;"
        )
        self._campaign_inner.insertWidget(0, self._empty_label)

    def _hide_empty_state(self):
        if self._empty_label is not None:
            self._empty_label.deleteLater()
            self._empty_label = None

    def _on_start_campaign(self):
        config = self._components.get("config")
        orch   = self._components.get("traffic_orchestrator")
        if not config or not orch:
            QMessageBox.warning(
                self, "Error",
                "System not ready. Please restart."
            )
            return
        target_urls = config.get("traffic.target_urls", [])
        if not target_urls:
            QMessageBox.warning(
                self,
                "No Targets",
                "❌ No target URLs configured!\n\n"
                "Go to Config → Targets tab\n"
                "Add URLs and click Save & Apply"
            )
            return
        proxy_engine = self._components.get("proxy_engine")
        if proxy_engine:
            proxy_required = bool(
                config.get("proxy.enabled", True)
                and config.get("proxy.required", True)
            )
            if (
                proxy_required
                and getattr(proxy_engine, "requires_socks_dependency", False)
                and not getattr(proxy_engine, "socks_dependency_available", True)
            ):
                QMessageBox.critical(
                    self,
                    "Missing SOCKS5 Dependency",
                    "❌ SOCKS5 support is not installed.\n\n"
                    "Your proxies are SOCKS5, so install dependencies first:\n\n"
                    "pip install -r requirements.txt\n\n"
                    "Campaign was blocked to prevent real-IP traffic.",
                )
                self.add_activity(
                    "Campaign blocked: aiohttp_socks missing for SOCKS5 proxies.",
                    "error",
                )
                return

            proxy_count = proxy_engine.available_count
            total_count = getattr(proxy_engine, "total_count", 0)
            if proxy_required and (total_count == 0 or proxy_count == 0):
                QMessageBox.critical(
                    self,
                    "Proxy Required",
                    "❌ No usable proxies are available.\n\n"
                    "Strict proxy mode is enabled, so the campaign was blocked "
                    "to prevent real-IP traffic.\n\n"
                    "Add working proxies in Config → Proxies, then click Save & Apply.",
                )
                self.add_activity(
                    "Campaign blocked: no usable proxies available.",
                    "error",
                )
                return
            if not proxy_required and proxy_count == 0:
                reply = QMessageBox.question(
                    self,
                    "No Proxies",
                    "⚠️  No proxies loaded!\n\n"
                    "Proxy strict mode is off, so traffic may use your real IP.\n\n"
                    "Continue anyway?",
                    QMessageBox.StandardButton.Yes |
                    QMessageBox.StandardButton.No,
                )
                if reply != QMessageBox.StandardButton.Yes:
                    return
        total_sessions = config.get(
            "traffic.total_sessions",
            config.get("traffic.sessions_per_hour", 30),
        )
        rate = config.get("traffic.sessions_per_hour", 30)
        try:
            total_sessions = int(total_sessions)
        except Exception:
            total_sessions = int(rate or 30)
        try:
            rate = int(rate)
        except Exception:
            rate = 30
        self._create_and_start_campaign(
            orch, target_urls, total_sessions, rate, config
        )

    def _create_and_start_campaign(
        self,
        orchestrator,
        target_urls: List[str],
        total: int,
        rate:  int,
        config,
    ):
        if not self._async_loop:
            logger.error("[Dashboard] No background loop available for start campaign.")
            return

        async def do_start():
            from datetime import datetime
            try:
                campaign = await orchestrator.create_campaign(
                    name = (
                        f"Campaign "
                        f"{datetime.now().strftime('%H:%M:%S')}"
                    ),
                    target_urls       = target_urls,
                    total_sessions    = total,
                    sessions_per_hour = rate,
                    organic_ratio  = config.get(
                        "traffic.organic_ratio", 0.60
                    ),
                    social_ratio   = config.get(
                        "traffic.social_ratio", 0.15
                    ),
                    direct_ratio   = config.get(
                        "traffic.direct_ratio", 0.15
                    ),
                    referral_ratio = config.get(
                        "traffic.referral_ratio", 0.10
                    ),
                    bounce_rate    = config.get(
                        "traffic.bounce_rate", 0.35
                    ),
                )
                started = await orchestrator.start_campaign(
                    campaign.campaign_id
                )
                if not started:
                    self._async_loop.call_soon_threadsafe(
                        self._on_campaign_failed_ui,
                        "Proxy preflight failed. No campaign was started."
                    )
                    return
                self._async_loop.call_soon_threadsafe(
                    self._on_campaign_started_ui, campaign.campaign_id, total
                )
            except Exception as exc:
                logger.error(f"[Dashboard] Start campaign error: {exc}", exc_info=True)
                self._async_loop.call_soon_threadsafe(
                    self._on_campaign_failed_ui, str(exc)
                )

        asyncio.run_coroutine_threadsafe(do_start(), self._async_loop)

    def _on_campaign_started_ui(self, cid, total):
        logger.info(f"[Dashboard] Campaign started: {cid}")
        config = self._components.get("config")
        interval = 0
        if config:
            try:
                interval = float(config.get("traffic.session_interval_seconds", 0) or 0)
            except Exception:
                interval = 0
        if interval > 0 and config and config.get("traffic.qa_mode", True):
            self.add_activity(
                f"🚀 Campaign started with {total} QA checks. Next checks run about every {interval:.0f}s.",
                "success",
            )
        else:
            self.add_activity(
                f"🚀 Campaign started with {total} sessions",
                "success",
            )

    def _on_campaign_failed_ui(self, error_msg):
        self.add_activity(f"❌ Start failed: {error_msg}", "error")

    def _on_stop_all(self):
        orch = self._components.get("traffic_orchestrator")
        if not orch or not self._async_loop:
            return
        campaigns = orch.get_all_campaigns()
        active = [c for c in campaigns if c.is_active]
        if not active:
            QMessageBox.information(
                self, "Stop All",
                "No active campaigns to stop."
            )
            return
        reply = QMessageBox.question(
            self,
            "Stop All Campaigns",
            f"Stop all {len(active)} active campaigns?",
            QMessageBox.StandardButton.Yes |
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
            
        async def do_stop_all():
            try:
                for c in active:
                    await orch.stop_campaign(
                        c.campaign_id, "user_stop_all"
                    )
                self._async_loop.call_soon_threadsafe(
                     self.add_activity, f"⏹️ Stopped {len(active)} campaigns", "warning"
                )
            except Exception as exc:
                logger.error(f"[Dashboard] Stop all error: {exc}")
        asyncio.run_coroutine_threadsafe(do_stop_all(), self._async_loop)

    def update_data(self, data: Dict[str, Any]):
        if not HAS_QT:
            return
        try:
            gm = data.get("global_metrics", {})
            pm = data.get("proxy_summary",  {})
            sm = data.get("session_metrics", {})
            self._tiles["sessions"].set_value(
                str(gm.get("active_workers", 0))
            )
            self._tiles["launched"].set_value(
                str(gm.get("total_verified_loads", gm.get("total_completed", 0)))
            )
            # Use the overall finished-session rate for the main tile. The
            # previous recent-only metric could show 100% after the latest
            # successful session even when earlier sessions failed in the same
            # campaign.
            sr = gm.get("overall_success_rate", gm.get("recent_success_rate", 0.0))
            self._tiles["success_rate"].set_value(
                f"{sr * 100:.1f}%"
            )
            self._tiles["proxies"].set_value(
                str(pm.get("available", 0))
            )
            self._tiles["detected"].set_value(
                str(gm.get("total_detected", 0))
            )
            self._tiles["sessions_hr"].set_value(
                str(int(sm.get("sessions_per_hour", 0)))
            )
            campaigns = data.get("campaigns", [])
            self._update_campaigns(campaigns)
            self._update_activity_from_metrics(gm, pm, campaigns)
        except Exception as exc:
            logger.debug(
                f"[Dashboard] Update error: {exc}"
            )

    def _update_campaigns(self, campaigns: List[Dict]):
        if not HAS_QT:
            return
        while self._campaign_inner.count():
            item = self._campaign_inner.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        if not campaigns:
            self._empty_label = None
            self._show_empty_state()
        else:
            self._empty_label = None
            for campaign_data in campaigns:
                card = CampaignCard(
                    campaign_data, self._components, self._async_loop
                )
                self._campaign_inner.addWidget(card)
            self._campaign_inner.addStretch()

    def _update_activity_from_metrics(self, gm: Dict[str, Any], pm: Dict[str, Any], campaigns: List[Dict]):
        """Show user-visible status changes in the Activity Feed."""
        try:
            proxies = int(pm.get("available", 0) or 0)
            if self._last_activity_state.get("proxies") != proxies:
                self._last_activity_state["proxies"] = proxies
                if proxies > 0:
                    self.add_activity(f"Proxy validation OK: {proxies} usable proxies", "success")
                else:
                    self.add_activity("No usable proxies available", "warning")

            verified = int(gm.get("total_verified_loads", 0) or 0)
            previous_verified = int(self._last_activity_state.get("verified", 0) or 0)
            if verified > previous_verified:
                self.add_activity(f"✅ Verified page load saved. Total verified: {verified}", "success")
            self._last_activity_state["verified"] = max(previous_verified, verified)

            failed = int(gm.get("total_failed", 0) or 0)
            previous_failed = int(self._last_activity_state.get("failed", 0) or 0)
            if failed > previous_failed:
                self.add_activity(f"⚠️ Session failed. Total failed: {failed}. Check reports/session_report.csv", "warning")
            self._last_activity_state["failed"] = max(previous_failed, failed)

            launched = int(gm.get("total_launched", 0) or 0)
            previous_launched = int(self._last_activity_state.get("launched", 0) or 0)
            if launched > previous_launched:
                self.add_activity(f"Browser launched for QA check. Total browser launches: {launched}", "info")
            self._last_activity_state["launched"] = max(previous_launched, launched)

            status_map = self._last_activity_state.setdefault("campaign_status", {})
            for campaign in campaigns:
                cid = campaign.get("campaign_id", "")
                status = campaign.get("status", "")
                old_status = status_map.get(cid)
                if cid and old_status != status:
                    status_map[cid] = status
                    if old_status is not None:
                        level = "success" if status == "completed" else "warning" if status in ("paused", "cancelled") else "error" if status == "failed" else "info"
                        if status == "completed":
                            launched = int(campaign.get("sessions_launched", 0) or 0)
                            completed = int(campaign.get("sessions_completed", 0) or 0)
                            failed_count = int(campaign.get("sessions_failed", 0) or 0)
                            self.add_activity(
                                f"Campaign {cid} completed: verified={completed}, failed={failed_count}, total={launched}",
                                level,
                            )
                        else:
                            self.add_activity(f"Campaign {cid} status: {status}", level)
        except Exception as exc:
            logger.debug(f"[Dashboard] Activity update error: {exc}")

    def add_activity(self, message: str, level: str = "info"):
        if not HAS_QT:
            return
        color_map = {
            "info":    "#e0e0e0",
            "success": "#4caf50",
            "warning": "#ff9800",
            "error":   "#f44336",
        }
        color = color_map.get(level, "#e0e0e0")
        ts = time.strftime("%H:%M:%S")
        label = QLabel(f"[{ts}] {message}")
        label.setStyleSheet(
            f"color: {color}; font-size: 11px; "
            f"padding: 3px 6px; "
            f"border-left: 3px solid {color}; "
            f"background: rgba(255,255,255,0.02);"
        )
        label.setWordWrap(True)
        self._activity_inner.insertWidget(0, label)
        while self._activity_inner.count() > 50:
            item = self._activity_inner.takeAt(
                self._activity_inner.count() - 1
            )
            if item.widget():
                item.widget().deleteLater()
