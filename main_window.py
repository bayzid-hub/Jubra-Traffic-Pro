"""
Jubra Traffic Pro - Main Window
Master GUI controller with tabbed interface,
system tray, and real-time updates.
"""
import os
import sys
import asyncio
import time
import logging
from typing import Any, Dict, Optional

os.environ["QT_QPA_PLATFORM"] = "windows"
try:
    from PyQt6.QtWidgets import (
        QApplication,
        QMainWindow,
        QTabWidget,
        QWidget,
        QVBoxLayout,
        QHBoxLayout,
        QStatusBar,
        QLabel,
        QPushButton,
        QMessageBox,
        QSplitter,
        QFrame,
        QSizePolicy,
        QMenuBar,
    )
    from PyQt6.QtCore import (
        Qt,
        QTimer,
        QThread,
        pyqtSignal,
        QSettings,
    )
    from PyQt6.QtGui import (
        QIcon,
        QFont,
        QPalette,
        QColor,
        QAction,
    )
    HAS_QT = True
except ImportError as exc:
    HAS_QT = False
    print(f"[GUI IMPORT ERROR] {exc}")

logger = logging.getLogger(__name__)

DARK_STYLESHEET = """
QMainWindow, QWidget {
    background-color: #1a1a2e;
    color: #e0e0e0;
    font-family: 'Segoe UI', sans-serif;
    font-size: 13px;
}
QTabWidget::pane {
    border: 1px solid #2d2d44;
    background-color: #16213e;
    border-radius: 4px;
}
QTabBar::tab {
    background-color: #1a1a2e;
    color: #9e9e9e;
    padding: 8px 20px;
    border: 1px solid #2d2d44;
    border-bottom: none;
    border-top-left-radius: 4px;
    border-top-right-radius: 4px;
    margin-right: 2px;
}
QTabBar::tab:selected {
    background-color: #0f3460;
    color: #e94560;
    border-bottom: 2px solid #e94560;
}
QTabBar::tab:hover {
    background-color: #0f3460;
    color: #ffffff;
}
QPushButton {
    background-color: #0f3460;
    color: #ffffff;
    border: 1px solid #e94560;
    border-radius: 4px;
    padding: 6px 16px;
    font-weight: bold;
}
QPushButton:hover {
    background-color: #e94560;
}
QPushButton:pressed {
    background-color: #c73652;
}
QPushButton:disabled {
    background-color: #2d2d44;
    color: #666666;
    border-color: #444444;
}
QLabel {
    color: #e0e0e0;
}
QStatusBar {
    background-color: #0f3460;
    color: #e0e0e0;
    border-top: 1px solid #e94560;
}
QMenuBar {
    background-color: #1a1a2e;
    color: #e0e0e0;
    border-bottom: 1px solid #2d2d44;
}
QMenuBar::item:selected {
    background-color: #0f3460;
}
QMenu {
    background-color: #16213e;
    border: 1px solid #2d2d44;
}
QMenu::item:selected {
    background-color: #0f3460;
    color: #e94560;
}
QFrame[frameShape="4"],
QFrame[frameShape="5"] {
    color: #2d2d44;
}
QSplitter::handle {
    background-color: #2d2d44;
}
"""

class AsyncBridge(QThread if HAS_QT else object):
    if HAS_QT:
        update_signal = pyqtSignal(dict)
        error_signal  = pyqtSignal(str)
        
    def __init__(self, components: Dict[str, Any], async_loop=None):
        if HAS_QT:
            super().__init__()
        self._components = components
        self._loop       = async_loop # Use the provided background loop
        self._running    = False
        
    def run(self):
        self._running = True
        try:
            # We don't create a new loop here anymore. We schedule the data collection
            # on the main background loop.
            asyncio.run_coroutine_threadsafe(self._main_loop(), self._loop)
        except Exception as exc:
            if HAS_QT:
                self.error_signal.emit(str(exc))
                
    async def _main_loop(self):
        while self._running:
            try:
                data = await self._collect_data()
                if HAS_QT:
                    self.update_signal.emit(data)
            except Exception as exc:
                logger.debug(f"[AsyncBridge] Data error: {exc}")
            await asyncio.sleep(1.0)
            
    async def _collect_data(self) -> Dict[str, Any]:
        data = {"timestamp": time.time()}
        try:
            orchestrator = self._components.get("traffic_orchestrator")
            if orchestrator:
                data["campaigns"] = [
                    c.to_dict()
                    for c in orchestrator.get_all_campaigns()
                ]
                data["global_metrics"] = (
                    orchestrator.get_global_metrics()
                )
            session_mgr = self._components.get("session_manager")
            if session_mgr:
                data["session_metrics"] = (
                    session_mgr.get_full_metrics()
                )
            proxy_engine = self._components.get("proxy_engine")
            if proxy_engine:
                data["proxy_summary"] = (
                    proxy_engine.get_pool_summary()
                )
            browser_farm = self._components.get("browser_farm")
            if browser_farm:
                data["browser_status"] = (
                    browser_farm.get_status()
                )
            healer = self._components.get("self_healing")
            if healer:
                data["health"] = healer.get_metrics()
        except Exception as exc:
            logger.debug(f"[AsyncBridge] Collect error: {exc}")
        return data
        
    def stop(self):
        self._running = False


class StatusWidget(QWidget if HAS_QT else object):
    def __init__(self):
        if HAS_QT:
            super().__init__()
            self._setup_ui()
            
    def _setup_ui(self):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 2, 8, 2)
        self._labels: Dict[str, QLabel] = {}
        items = [
            ("sessions",        "Sessions: 0/0"),
            ("proxies",         "Proxies: 0"),
            ("success_rate",    "Success: 0%"),
            ("detection",       "Detected: 0"),
            ("uptime",          "Uptime: 00:00:00"),
        ]
        for key, text in items:
            lbl = QLabel(text)
            lbl.setStyleSheet(
                "color: #9e9e9e; font-size: 12px;"
            )
            self._labels[key] = lbl
            layout.addWidget(lbl)
            if key != items[-1][0]:
                sep = QLabel("|")
                sep.setStyleSheet("color: #2d2d44;")
                layout.addWidget(sep)
        layout.addStretch()
        self._status_dot = QLabel("●")
        self._status_dot.setStyleSheet(
            "color: #4caf50; font-size: 16px;"
        )
        layout.addWidget(self._status_dot)
        self._status_text = QLabel("Running")
        self._status_text.setStyleSheet(
            "color: #4caf50; font-size: 12px;"
        )
        layout.addWidget(self._status_text)
        
    def update_data(self, data: Dict[str, Any]):
        if not HAS_QT:
            return
        gm = data.get("global_metrics", {})
        pm = data.get("proxy_summary",  {})
        sm = data.get("session_metrics", {})
        if "sessions" in self._labels:
            self._labels["sessions"].setText(
                f"Sessions: "
                f"{gm.get('active_workers', 0)}/"
                f"{gm.get('total_launched', 0)}"
            )
        if "proxies" in self._labels:
            self._labels["proxies"].setText(
                f"Proxies: {pm.get('available', 0)}"
            )
        if "success_rate" in self._labels:
            sr = sm.get("success_rate", 1.0)
            self._labels["success_rate"].setText(
                f"Success: {sr * 100:.1f}%"
            )
        if "detection" in self._labels:
            self._labels["detection"].setText(
                f"Detected: {gm.get('total_detected', 0)}"
            )


class MainWindow(QMainWindow if HAS_QT else object):
    """
    Jubra Traffic Pro - Main GUI Window.
    Tabs: Dashboard | Logs | Config | Charts
    """
    def __init__(
        self,
        components:  Dict[str, Any],
        ring_buffer: Any = None,
        async_loop: Any = None, # Accept the loop here
        parent       = None,
    ):
        if HAS_QT:
            super().__init__(parent)
        self._components  = components
        self._ring_buffer = ring_buffer
        self._async_loop = async_loop
        self._start_time  = time.monotonic()
        if HAS_QT:
            self._setup_window()
            self._setup_menu()
            self._setup_tabs()
            self._setup_status_bar()
            self._setup_async_bridge()
            self._setup_update_timer()
            
    def _setup_window(self):
        self.setWindowTitle("Jubra Traffic Pro")
        self.setMinimumSize(1200, 750)
        self.resize(1400, 900)
        self.setStyleSheet(DARK_STYLESHEET)
        screen = QApplication.primaryScreen()
        if screen:
            geo = screen.availableGeometry()
            x   = (geo.width()  - 1400) // 2
            y   = (geo.height() - 900)  // 2
            self.move(x, y)
            
    def _setup_menu(self):
        menubar = self.menuBar()
        file_menu = menubar.addMenu("File")
        start_action = QAction("Start Campaign", self)
        start_action.setShortcut("Ctrl+S")
        start_action.triggered.connect(self._on_start)
        file_menu.addAction(start_action)
        stop_action = QAction("Stop All", self)
        stop_action.setShortcut("Ctrl+Q")
        stop_action.triggered.connect(self._on_stop_all)
        file_menu.addAction(stop_action)
        file_menu.addSeparator()
        exit_action = QAction("Exit", self)
        exit_action.setShortcut("Alt+F4")
        exit_action.triggered.connect(self.close)
        file_menu.addAction(exit_action)
        view_menu = menubar.addMenu("View")
        dark_action = QAction("Dark Theme", self)
        dark_action.triggered.connect(
            lambda: self.setStyleSheet(DARK_STYLESHEET)
        )
        view_menu.addAction(dark_action)
        help_menu = menubar.addMenu("Help")
        about_action = QAction("About", self)
        about_action.triggered.connect(self._show_about)
        help_menu.addAction(about_action)
        
    def _setup_tabs(self):
        self._tabs = QTabWidget()
        self._tabs.setTabPosition(
            QTabWidget.TabPosition.North
        )
        try:
            from gui.dashboard    import Dashboard
            from gui.log_viewer   import LogViewer
            from gui.config_panel import ConfigPanel
            from gui.charts       import Charts
            # Pass the async_loop to the Dashboard
            self._dashboard = Dashboard(self._components, async_loop=self._async_loop)
            self._log_view  = LogViewer(self._ring_buffer)
            self._config    = ConfigPanel(self._components)
            self._charts    = Charts()
            self._tabs.addTab(
                self._dashboard, "📊 Dashboard"
            )
            self._tabs.addTab(
                self._log_view, "📋 Logs"
            )
            self._tabs.addTab(
                self._config, "⚙️  Config"
            )
            self._tabs.addTab(
                self._charts, "📈 Charts"
            )
        except Exception as exc:
            logger.error(
                f"[MainWindow] Tab setup error: {exc}"
            )
            placeholder = QWidget()
            layout = QVBoxLayout(placeholder)
            lbl = QLabel(
                f"Tab load error: {exc}\n\n"
                f"Core system is running correctly."
            )
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lbl.setStyleSheet("color: #ff9800;")
            layout.addWidget(lbl)
            self._tabs.addTab(placeholder, "⚠️ Error")
        self.setCentralWidget(self._tabs)
        
    def _setup_status_bar(self):
        self._status_widget = StatusWidget()
        self.statusBar().addPermanentWidget(
            self._status_widget, 1
        )
        self.statusBar().showMessage("Ready", 3000)
        
    def _setup_async_bridge(self):
        self._bridge = AsyncBridge(self._components, self._async_loop)
        if HAS_QT:
            self._bridge.update_signal.connect(
                self._on_data_update
            )
            self._bridge.error_signal.connect(
                self._on_error
            )
            self._bridge.start()
            
    def _setup_update_timer(self):
        self._timer = QTimer()
        self._timer.timeout.connect(self._on_tick)
        self._timer.start(1000)
        
    def _on_data_update(self, data: Dict[str, Any]):
        try:
            self._status_widget.update_data(data)
            if hasattr(self, "_dashboard"):
                self._dashboard.update_data(data)
            if hasattr(self, "_charts"):
                self._charts.update_data(data)
        except Exception as exc:
            logger.debug(
                f"[MainWindow] Update error: {exc}"
            )
            
    def _on_tick(self):
        uptime = time.monotonic() - self._start_time
        if hasattr(self, "_status_widget"):
            label = self._status_widget._labels.get("uptime")
            if label:
                h = int(uptime // 3600)
                m = int((uptime % 3600) // 60)
                s = int(uptime % 60)
                label.setText(
                    f"Uptime: {h:02d}:{m:02d}:{s:02d}"
                )
                
    def _on_start(self):
        self.statusBar().showMessage(
            "Starting campaign...", 2000
        )
        
    def _on_stop_all(self):
        reply = QMessageBox.question(
            self,
            "Stop All",
            "Stop all running campaigns?",
            QMessageBox.StandardButton.Yes |
            QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self.statusBar().showMessage(
                "Stopping all campaigns...", 2000
            )
            
    def _on_error(self, error: str):
        logger.error(f"[MainWindow] Async error: {error}")
        
    def _show_about(self):
        QMessageBox.about(
            self,
            "About Jubra Traffic Pro",
            "<h2>Jubra Traffic Pro</h2>"
            "<p>Professional Web Analytics Solution</p>"
            "<p>Version: 1.0.0</p>"
            "<p>Engine: nodriver (No ChromeDriver)</p>",
        )
        
    def closeEvent(self, event):
        if hasattr(self, "_bridge"):
            self._bridge.stop()
            self._bridge.wait(3000)
        if hasattr(self, "_timer"):
            self._timer.stop()
        event.accept()

def launch_gui(
    components:  Dict[str, Any],
    ring_buffer: Any = None,
    async_loop: Any = None
) -> None:
    """Launch the GUI application in the main thread."""
    if not HAS_QT:
        logger.error(
            "PyQt6 not available. pip install PyQt6"
        )
        return
    # Prevent duplicate QApplication instances
    existing = QApplication.instance()
    if existing is not None:
        app = existing
    else:
        app = QApplication(sys.argv)
        app.setApplicationName("Jubra Traffic Pro")
        app.setStyle("Fusion")
        
    window = MainWindow(components, ring_buffer, async_loop)
    window.show()
    window.raise_()
    window.activateWindow()
    app.exec()
