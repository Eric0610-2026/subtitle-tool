#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
自定义 Qt 控件：DropListWidget（拖放文件列表）和 LogEntry（日志条目）
"""
import logging
from pathlib import Path

from PySide6.QtCore import Qt, Signal, QEvent, QObject, QPropertyAnimation, QEasingCurve, QAbstractAnimation
from PySide6.QtGui import QFont, QDragEnterEvent, QDragMoveEvent, QDropEvent
from PySide6.QtWidgets import (
    QListWidget, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QApplication, QAbstractItemView,
)

from .config import cfg
from .srt_utils import SUB_EXTS

logger = logging.getLogger(__name__)

# 扫描的视频/音频扩展（排除 config.app.scan_skip_exts 中指定的格式）
SCAN_VIDEO_EXTS = set(cfg.srt.video_exts) - set(cfg.app.scan_skip_exts)
AUDIO_EXTS = set(getattr(cfg.srt, "audio_exts", []))

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

    def _set_drag_over(self, on: bool):
        """切换拖入高亮态（QSS 属性选择器 QListWidget[dragOver="true"]）"""
        if self.property("dragOver") == on:
            return
        self.setProperty("dragOver", on)
        self.style().unpolish(self)
        self.style().polish(self)

    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            self._set_drag_over(True)
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

    def dragLeaveEvent(self, event):
        self._set_drag_over(False)
        super().dragLeaveEvent(event)

    def dropEvent(self, event: QDropEvent):
        self._set_drag_over(False)
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


class PopupFade(QObject):
    """QComboBox 弹层增强：淡入过渡动画 + 去除原生灰底。

    QSS 不支持 transition；弹层容器是独立原生窗口：
    - 灰底：Windows 在 Qt 绘制前会先用系统灰擦除原生窗口背景，视图的
      圆角缺口处会露出灰边 → 设置 WA_TranslucentBackground 让圆角外真透明；
    - 动画：监听容器 Show 事件，对 windowOpacity 做 0→1 短动画（OutCubic）。
    """

    def __init__(self, combo, duration_ms: int = 130):
        super().__init__(combo)  # 挂在 combo 下，随控件销毁
        container = combo.view().window()
        # 必须在原生窗口首次显示前设置（attach 时弹层尚未创建过窗口）
        container.setAttribute(Qt.WA_TranslucentBackground, True)
        container.installEventFilter(self)
        self._duration = duration_ms

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Show:
            obj.setWindowOpacity(0.0)  # 首帧前置零，避免闪一帧全不透明
            anim = QPropertyAnimation(obj, b"windowOpacity", obj)
            anim.setDuration(self._duration)
            anim.setStartValue(0.0)
            anim.setEndValue(1.0)
            anim.setEasingCurve(QEasingCurve.OutCubic)
            anim.start(QAbstractAnimation.DeletionPolicy.DeleteWhenStopped)
        return False


def attach_popup_fade(combo, duration_ms: int = 130) -> None:
    """为下拉框启用弹层淡入动画（见 PopupFade）"""
    PopupFade(combo, duration_ms)


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
        self.message = message  # 完整消息（含时间戳前缀），导出/复制用
        self.trace = trace
        tag_bg, tag_fg = self._LEVEL_STYLE.get(level, self._LEVEL_STYLE["INFO"])
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 3, 6, 3)
        layout.setSpacing(1)
        top = QHBoxLayout()
        top.setSpacing(8)
        tag = QLabel(level)
        tag.setFixedWidth(56)
        tag.setAlignment(Qt.AlignCenter)
        tag.setStyleSheet(
            f"color:{tag_fg}; background:{tag_bg}; border-radius:3px; "
            "font-size:10px; font-weight:600; padding:1px 2px;")
        top.addWidget(tag)
        # 展示层把时间戳拆出来弱化，正文优先扫读；message 属性保持完整前缀
        ts = ""
        display = message
        if message.startswith("[") and "]" in message[:12]:
            head, _, rest = message.partition("]")
            ts = head[1:]
            display = rest.lstrip()
        if ts:
            ts_label = QLabel(ts)
            ts_label.setFont(QFont("Consolas", 9))
            ts_label.setStyleSheet("color:#64748b;")
            top.addWidget(ts_label)
        self.msg_label = QLabel(display)
        self.msg_label.setWordWrap(True)
        self.msg_label.setFont(QFont("Consolas", 10))
        top.addWidget(self.msg_label, 1)
        if trace:
            self._trace_visible = False
            self._list_item = None
            self.toggle_btn = QPushButton("▶")
            self.toggle_btn.setFixedSize(24, 20)
            self.toggle_btn.setStyleSheet("padding:0; font-size:10px;")
            self.toggle_btn.clicked.connect(self._toggle)
            top.addWidget(self.toggle_btn)
        self.copy_btn = QPushButton("📋")
        self.copy_btn.setFixedSize(24, 20)
        self.copy_btn.setStyleSheet("padding:0; font-size:11px;")
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

    def sizeHint(self):
        """在布局建议高度上再加余量：QSS 的 item 内边距不参与 sizeHint 计算，
        且 emoji/中文字形的实际渲染高度普遍超出字体 metrics，
        按 Raw sizeHint 设行高会把文字和图标底部裁掉。"""
        hint = super().sizeHint()
        hint.setHeight(hint.height() + 10)
        return hint

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
