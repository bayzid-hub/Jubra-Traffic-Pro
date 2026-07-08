"""
Jubra Traffic Pro - Log Viewer
Live log streaming with filtering, search, and export.
"""

import time
import logging
from typing import Any, Optional

try:
    from PyQt6.QtWidgets import (
        QWidget, QVBoxLayout, QHBoxLayout,
        QTextEdit, QPushButton, QComboBox,
        QLineEdit, QLabel, QCheckBox,
        QFileDialog,
    )
    from PyQt6.QtCore import Qt, QTimer
    from PyQt6.QtGui import QFont, QTextCursor, QColor, QTextCharFormat
    HAS_QT = True
except ImportError:
    HAS_QT = False

logger = logging.getLogger(__name__)


class LogViewer(QWidget if HAS_QT else object):
    """
    Live Log Viewer.

    Features:
    ─────────────────────────────────────────────────────
    • Real-time log streaming from RingBufferHandler
    • Level filtering (DEBUG/INFO/WARNING/ERROR)
    • Text search with highlight
    • Auto-scroll toggle
    • Log export to file
    • Color-coded log levels
    • Module filter
    • Log count display
    """

    LEVEL_COLORS = {
        "DEBUG":    "#9e9e9e",
        "INFO":     "#4caf50",
        "WARNING":  "#ff9800",
        "ERROR":    "#f44336",
        "CRITICAL": "#e91e63",
    }

    def __init__(self, ring_buffer: Any = None):
        if HAS_QT:
            super().__init__()
        self._ring_buffer   = ring_buffer
        self._auto_scroll   = True
        self._last_count    = 0
        if HAS_QT:
            self._setup_ui()
            self._setup_timer()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        # ── Toolbar ───────────────────────────────────────
        toolbar = QHBoxLayout()

        # Level filter
        toolbar.addWidget(QLabel("Level:"))
        self._level_filter = QComboBox()
        self._level_filter.addItems(
            ["ALL", "DEBUG", "INFO", "WARNING", "ERROR"]
        )
        self._level_filter.setCurrentText("INFO")
        self._level_filter.setFixedWidth(100)
        self._level_filter.setStyleSheet("""
            QComboBox {
                background-color: #16213e;
                color: #e0e0e0;
                border: 1px solid #2d2d44;
                border-radius: 4px;
                padding: 4px;
            }
            QComboBox::drop-down { border: none; }
            QComboBox QAbstractItemView {
                background-color: #16213e;
                color: #e0e0e0;
                selection-background-color: #0f3460;
            }
        """)
        toolbar.addWidget(self._level_filter)

        # Search
        toolbar.addWidget(QLabel("Search:"))
        self._search_box = QLineEdit()
        self._search_box.setPlaceholderText("Filter logs...")
        self._search_box.setFixedWidth(200)
        self._search_box.setStyleSheet("""
            QLineEdit {
                background-color: #16213e;
                color: #e0e0e0;
                border: 1px solid #2d2d44;
                border-radius: 4px;
                padding: 4px 8px;
            }
        """)
        toolbar.addWidget(self._search_box)

        toolbar.addStretch()

        # Auto-scroll
        self._autoscroll_cb = QCheckBox("Auto-scroll")
        self._autoscroll_cb.setChecked(True)
        self._autoscroll_cb.stateChanged.connect(
            lambda s: setattr(self, "_auto_scroll", bool(s))
        )
        toolbar.addWidget(self._autoscroll_cb)

        # Clear button
        clear_btn = QPushButton("Clear")
        clear_btn.setFixedWidth(80)
        clear_btn.clicked.connect(self._clear_logs)
        toolbar.addWidget(clear_btn)

        # Export button
        export_btn = QPushButton("Export")
        export_btn.setFixedWidth(80)
        export_btn.clicked.connect(self._export_logs)
        toolbar.addWidget(export_btn)

        layout.addLayout(toolbar)

        # ── Log Display ───────────────────────────────────
        self._log_display = QTextEdit()
        self._log_display.setReadOnly(True)
        self._log_display.setFont(
            QFont("Consolas, Courier New, monospace", 11)
        )
        self._log_display.setStyleSheet("""
            QTextEdit {
                background-color: #0d0d1a;
                color: #e0e0e0;
                border: 1px solid #2d2d44;
                border-radius: 4px;
                padding: 8px;
                selection-background-color: #0f3460;
            }
        """)
        layout.addWidget(self._log_display, 1)

        # ── Status bar ────────────────────────────────────
        self._count_label = QLabel("Logs: 0")
        self._count_label.setStyleSheet("color: #9e9e9e; font-size: 11px;")
        layout.addWidget(self._count_label)

    def _setup_timer(self):
        self._refresh_timer = QTimer()
        self._refresh_timer.timeout.connect(self._refresh_logs)
        self._refresh_timer.start(500)  # Every 500ms

    def _refresh_logs(self):
        """Pull new entries from ring buffer and display."""
        if not self._ring_buffer or not HAS_QT:
            return

        try:
            level_filter    = self._level_filter.currentText()
            search_text     = self._search_box.text().lower()

            entries = self._ring_buffer.get_entries(
                count           = 500,
                level_filter    = None if level_filter == "ALL" else level_filter,
                search          = search_text if search_text else None,
            )

            if len(entries) == self._last_count:
                return

            self._last_count = len(entries)

            # Rebuild log display
            self._log_display.clear()
            cursor = self._log_display.textCursor()

            for entry in entries[-200:]:
                color   = self.LEVEL_COLORS.get(entry.level, "#e0e0e0")
                ts      = time.strftime(
                    "%H:%M:%S",
                    time.localtime(entry.timestamp)
                )

                # Format line
                line = (
                    f"[{ts}] [{entry.level:8s}] "
                    f"[{entry.module[:20]:20s}] {entry.message}\n"
                )

                # Apply color
                fmt = QTextCharFormat()
                fmt.setForeground(QColor(color))
                cursor.insertText(line, fmt)

            self._count_label.setText(f"Logs: {len(entries)}")

            # Auto-scroll
            if self._auto_scroll:
                scrollbar = self._log_display.verticalScrollBar()
                scrollbar.setValue(scrollbar.maximum())

        except Exception as exc:
            logger.debug(f"[LogViewer] Refresh error: {exc}")

    def _clear_logs(self):
        """Clear the log display."""
        if HAS_QT:
            self._log_display.clear()
            if self._ring_buffer:
                self._ring_buffer.clear()
            self._last_count = 0

    def _export_logs(self):
        """Export logs to a text file."""
        if not HAS_QT:
            return

        filepath, _ = QFileDialog.getSaveFileName(
            self, "Export Logs", "logs/export.log",
            "Log Files (*.log *.txt)"
        )
        if filepath and self._ring_buffer:
            entries = self._ring_buffer.get_entries(count=10000)
            with open(filepath, "w", encoding="utf-8") as f:
                for entry in entries:
                    f.write(entry.to_colored_string() + "\n")
            logger.info(f"Logs exported to: {filepath}")