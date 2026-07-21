#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
自定义 Qt 控件：DropListWidget（拖放文件列表）和 LogEntry（日志条目）
"""
import logging
from pathlib import Path
from typing import List

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont, QDragEnterEvent, QDragMoveEvent, QDropEvent
from PySide6.QtWidgets import (
    QListWidget, QListWidgetItem, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QApplication, QAbstractItemView,
)

from .config import cfg
from .srt_utils import SUB_EXTS

logger = logging.getLogger(__name__)

# 扫描的视频/音频扩展（排除 config.app.scan_skip_exts 中指定的格式）
SCAN_VIDEO_EXTS = set(cfg.srt.video_exts) - set(cfg.app.scan_skip_exts)
AUDIO_EXTS = set(getattr(cfg.srt, "audio_exts", []))


def is_audio_file(p: Path) -> bool:
    return p.suffix.lower() in AUDIO_EXTS


class DropListWidget(QListWidget):
    """支持拖放添加文件 + 内部拖放排序的列表控件"""
    dropped = Signal(list, bool)  # paths, is_video（外部拖入文件）
    reordered = Signal()          # 内部排序后通知同步 jobs

    def __init__(self, is_video_tab: bool, parent=None):
        super().__init__(parent)
        self._is_video = is_video_tab
        self.setAcceptDrops(True)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setDragDropMode(QAbstractItemView.InternalMove)

    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        elif event.source() is self:
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event: QDragMoveEvent):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        elif event.source() is self:
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event: QDropEvent):
        if event.mimeData().hasUrls():
            event.accept()
            paths = []
            for url in event.mimeData().urls():
                p = Path(url.toLocalFile())
                if p.is_file():
                    paths.append(p)
                elif p.is_dir():
                    exts = SCAN_VIDEO_EXTS | AUDIO_EXTS if self._is_video else SUB_EXTS
                    for f in sorted(p.iterdir()):
                        if f.is_file() and f.suffix.lower() in exts:
                            paths.append(f)
            if paths:
                self.dropped.emit(paths, self._is_video)
        else:
            super().dropEvent(event)
            self.reordered.emit()


class LogEntry(QWidget):
    """单条日志：级别色块 + 消息 + 可选可折叠 traceback"""

    _LEVEL_STYLE = {
        "DEBUG":   ("#64748b", "#475569"),
        "INFO":    ("#94a3b8", "#334155"),
        "WARNING": ("#fbbf24", "#b45309"),
        "ERROR":   ("#ef4444", "#b91c1c"),
    }

    def __init__(self, message, level="INFO", trace=None, parent=None):
        super().__init__(parent)
        level = (level or "INFO").upper()
        self.level = level
        self.message = message
        self.trace = trace
        tag_bg, tag_fg = self._LEVEL_STYLE.get(level, self._LEVEL_STYLE["INFO"])
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 1, 6, 1)
        layout.setSpacing(1)
        top = QHBoxLayout()
        tag = QLabel(level)
        tag.setFixedWidth(56)
        tag.setAlignment(Qt.AlignCenter)
        tag.setStyleSheet(
            f"color:{tag_fg}; background:{tag_bg}; border-radius:3px; "
            f"font-size:10px; font-weight:600; padding:1px 2px;")
        top.addWidget(tag)
        self.msg_label = QLabel(message)
        self.msg_label.setWordWrap(True)
        self.msg_label.setFont(QFont("Consolas", 10))
        top.addWidget(self.msg_label, 1)
        if trace:
            self._trace_visible = False
            self._list_item = None
            self.toggle_btn = QPushButton("▶")
            self.toggle_btn.setFixedSize(22, 18)
            self.toggle_btn.setStyleSheet("padding:0; font-size:10px;")
            self.toggle_btn.clicked.connect(self._toggle)
            top.addWidget(self.toggle_btn)
        self.copy_btn = QPushButton("📋")
        self.copy_btn.setFixedSize(22, 18)
        self.copy_btn.setStyleSheet("padding:0; font-size:10px;")
        self.copy_btn.setToolTip("复制")
        self.copy_btn.clicked.connect(self._copy)
        top.addWidget(self.copy_btn)
        top.addStretch(0)
        layout.addLayout(top)
        if trace:
            self.trace_label = QLabel(trace.rstrip())
            self.trace_label.setFont(QFont("Consolas", 9))
            self.trace_label.setStyleSheet("color:#ef4444;")
            self.trace_label.setWordWrap(True)
            self.trace_label.setVisible(False)
            layout.addWidget(self.trace_label)

    def _toggle(self):
        self._trace_visible = not self._trace_visible
        self.trace_label.setVisible(self._trace_visible)
        self.toggle_btn.setText("▾" if self._trace_visible else "▶")
        if self._list_item is not None:
            self._list_item.setSizeHint(self.sizeHint())

    def _copy(self):
        text = self.message
        if self.trace:
            text += "\n" + self.trace.rstrip()
        QApplication.clipboard().setText(text)
