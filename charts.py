"""
Jubra Traffic Pro - Real-Time Charts
Performance and traffic charts using pyqtgraph.
"""

import time
import logging
from collections import deque
from typing import Any, Dict, List

try:
    from PyQt6.QtWidgets import (
        QWidget, QVBoxLayout, QHBoxLayout,
        QLabel, QComboBox, QGridLayout,
    )
    from PyQt6.QtCore import Qt
    HAS_QT = True
except ImportError:
    HAS_QT = False

try:
    import pyqtgraph as pg
    import numpy as np
    HAS_PG = True
except ImportError:
    HAS_PG = False

logger = logging.getLogger(__name__)


class TimeSeriesChart(QWidget if HAS_QT else object):
    """A single real-time time series chart."""

    def __init__(
        self,
        title:      str,
        y_label:    str     = "",
        color:      str     = "#e94560",
        max_points: int     = 300,
    ):
        if HAS_QT:
            super().__init__()
        self._data      = deque(maxlen=max_points)
        self._times     = deque(maxlen=max_points)
        self._color     = color
        self._start_ts  = time.monotonic()

        if HAS_QT and HAS_PG:
            self._setup_chart(title, y_label, color)
        elif HAS_QT:
            self._setup_placeholder(title)

    def _setup_chart(self, title, y_label, color):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        pg.setConfigOption("background", "#0d0d1a")
        pg.setConfigOption("foreground", "#e0e0e0")

        self._plot_widget = pg.PlotWidget(title=title)
        self._plot_widget.setLabel("left",   y_label)
        self._plot_widget.setLabel("bottom", "Time (s)")
        self._plot_widget.showGrid(x=True, y=True, alpha=0.2)
        self._plot_widget.getAxis("bottom").setStyle(
            tickTextOffset=5
        )

        self._curve = self._plot_widget.plot(
            pen=pg.mkPen(color=color, width=2),
        )

        layout.addWidget(self._plot_widget)

    def _setup_placeholder(self, title):
        layout  = QVBoxLayout(self)
        label   = QLabel(
            f"{title}\n(pyqtgraph not installed)"
        )
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setStyleSheet("color: #666666;")
        layout.addWidget(label)

    def add_point(self, value: float):
        """Add a new data point."""
        now = time.monotonic() - self._start_ts
        self._times.append(now)
        self._data.append(value)

        if HAS_PG and hasattr(self, "_curve"):
            self._curve.setData(
                list(self._times),
                list(self._data),
            )


class Charts(QWidget if HAS_QT else object):
    """
    Performance Charts Tab.

    Charts:
    ─────────────────────────────────────────────────────
    • Sessions per minute (real-time)
    • Success rate over time
    • Detection rate over time
    • Proxy health score
    • Browser pool utilization
    • System CPU/Memory
    """

    def __init__(self):
        if HAS_QT:
            super().__init__()
            self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)

        # Header
        header = QLabel("📈 Performance Charts")
        header.setStyleSheet(
            "font-size: 18px; font-weight: bold; color: #e94560;"
        )
        layout.addWidget(header)

        if not HAS_PG:
            notice = QLabel(
                "⚠️  Install pyqtgraph for charts:\n"
                "pip install pyqtgraph numpy"
            )
            notice.setAlignment(Qt.AlignmentFlag.AlignCenter)
            notice.setStyleSheet(
                "color: #ff9800; font-size: 14px; padding: 20px;"
            )
            layout.addWidget(notice)
            return

        # Grid of charts
        grid = QGridLayout()
        grid.setSpacing(8)

        self._charts: Dict[str, TimeSeriesChart] = {
            "sessions_per_min": TimeSeriesChart(
                "Sessions / Minute", "count", "#e94560"
            ),
            "success_rate": TimeSeriesChart(
                "Success Rate %", "%", "#4caf50"
            ),
            "detection_rate": TimeSeriesChart(
                "Detection Rate %", "%", "#9c27b0"
            ),
            "proxy_count": TimeSeriesChart(
                "Available Proxies", "count", "#ff9800"
            ),
            "browser_pool": TimeSeriesChart(
                "Browser Pool Used", "count", "#2196f3"
            ),
            "cpu_memory": TimeSeriesChart(
                "CPU %", "%", "#00bcd4"
            ),
        }

        positions = [
            (0, 0), (0, 1),
            (1, 0), (1, 1),
            (2, 0), (2, 1),
        ]

        for (chart_name, chart), (row, col) in zip(
            self._charts.items(), positions
        ):
            grid.addWidget(chart, row, col)

        layout.addLayout(grid, 1)

    def update_data(self, data: Dict[str, Any]):
        """Update all charts with new data."""
        if not HAS_QT or not HAS_PG:
            return

        try:
            gm  = data.get("global_metrics", {})
            sm  = data.get("session_metrics", {})
            pm  = data.get("proxy_summary", {})
            bm  = data.get("browser_status", {})

            charts = getattr(self, "_charts", {})

            if "sessions_per_min" in charts:
                charts["sessions_per_min"].add_point(
                    sm.get("sessions_per_hour", 0) / 60
                )
            if "success_rate" in charts:
                charts["success_rate"].add_point(
                    sm.get("success_rate", 1.0) * 100
                )
            if "detection_rate" in charts:
                charts["detection_rate"].add_point(
                    sm.get("detection_rate", 0.0) * 100
                )
            if "proxy_count" in charts:
                charts["proxy_count"].add_point(
                    pm.get("available", 0)
                )
            if "browser_pool" in charts:
                charts["browser_pool"].add_point(
                    bm.get("in_use", 0)
                )

        except Exception as exc:
            logger.debug(f"[Charts] Update error: {exc}")