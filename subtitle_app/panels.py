#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
子面板组件：从 qt_app.py 拆分出的独立 UI 部件。

包含：
- ProgressPanel   — 进度条组（总进度、转写、翻译、ETA）
- PreviewPanel     — 字幕预览/编辑/查找/偏移
- LogPanel         — 日志列表面板
- SignalBridge     — 工作线程 → Qt 主线程的信号桥
"""
import logging
import queue
import time
from pathlib import Path
from typing import List, Optional

from PySide6.QtCore import Qt, QObject, Signal, QTimer
from PySide6.QtWidgets import (
    QGroupBox, QVBoxLayout, QHBoxLayout, QLabel, QProgressBar,
    QTextEdit, QListWidget, QListWidgetItem, QWidget, QPushButton,
    QFrame, QSizePolicy,
)
from PySide6.QtGui import QFont, QFontMetrics

from .srt_utils import fmt_duration, estimate_eta
from .widgets import LogEntry

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════
# ProgressPanel
# ═══════════════════════════════════════════════════════════════════

class ProgressPanel(QGroupBox):
    """总进度 + 转写/翻译子进度 + ETA + 语言/计数器"""

    def __init__(self, parent=None):
        super().__init__("进度", parent)
        self._start_time: Optional[float] = None
        self._build_ui()

    def _build_sub_group(self, name: str) -> tuple:
        """创建子进度条组，返回 (label, progress, detail) 三件套"""
        g = QGroupBox(name)
        v = QVBoxLayout(g)
        v.setContentsMargins(8, 6, 8, 6)
        label = QLabel("等待中")
        label.setStyleSheet("font-weight:600;")
        label.setMinimumWidth(10)
        label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        v.addWidget(label)
        bar = QProgressBar()
        bar.setRange(0, 100)
        bar.setValue(0)
        bar.setFixedHeight(20)
        bar.setTextVisible(True)
        bar.setFormat("")
        v.addWidget(bar)
        detail = QLabel("")
        detail.setMinimumWidth(10)
        detail.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        v.addWidget(detail)
        return g, label, bar, detail

    def _build_ui(self):
        layout = QVBoxLayout(self)

        # 总进度
        self.overall_label = QLabel("总进度：等待中")
        self.overall_label.setStyleSheet("font-weight:600; color:#6366f1;")
        layout.addWidget(self.overall_label)
        self.overall_progress = QProgressBar()
        self.overall_progress.setRange(0, 100)
        self.overall_progress.setValue(0)
        self.overall_progress.setFixedHeight(16)
        self.overall_progress.setTextVisible(True)
        self.overall_progress.setFormat("%p%")
        layout.addWidget(self.overall_progress)

        # 语言 / 计数器
        top = QHBoxLayout()
        self.lang_label = QLabel("语言：auto")
        top.addWidget(self.lang_label)
        top.addStretch()
        self.counter_label = QLabel("已转写 0/0 | 已翻译 0/0 | 缓存 0")
        top.addWidget(self.counter_label)
        layout.addLayout(top)

        # 子进度
        dual = QVBoxLayout()
        self._transcribe_group, self.transcribe_label, self.transcribe_bar, self.transcribe_detail = \
            self._build_sub_group("转写")
        self._translate_group, self.translate_label, self.translate_bar, self.translate_detail = \
            self._build_sub_group("翻译")
        dual.addWidget(self._transcribe_group, 1)
        dual.addWidget(self._translate_group, 1)
        layout.addLayout(dual)

        # 时间信息
        bot = QHBoxLayout()
        self.detail_label = QLabel("已用 --:-- | 剩余 --:-- | 预计 --")
        bot.addWidget(self.detail_label, 1)
        layout.addLayout(bot)

    # ── 公共更新方法 ──

    def reset(self):
        self.overall_progress.setValue(0)
        self.overall_label.setText("总进度：等待中")
        self.transcribe_bar.setValue(0)
        self.transcribe_bar.setFormat("")
        self.transcribe_label.setText("等待中")
        self.transcribe_detail.setText("")
        self.translate_bar.setValue(0)
        self.translate_bar.setFormat("")
        self.translate_label.setText("等待中")
        self.translate_detail.setText("")
        self.detail_label.setText("")
        self.lang_label.setText("语言：auto")
        self.counter_label.setText("已转写 0/0 | 已翻译 0/0 | 缓存 0")

    def set_overall(self, pct: float, text: str):
        self.overall_progress.setValue(int(pct))
        self.overall_label.setText(text)

    def set_language(self, lang: str):
        self.lang_label.setText(f"语言：{lang}")

    def set_counter(self, generated: int, translated: int, total: int, cache: int = 0):
        self.counter_label.setText(
            f"已转写 {generated}/{total} | 已翻译 {translated}/{total} | 缓存 {cache}")

    def set_detail(self, text: str):
        self.detail_label.setText(text)

    def set_sub_progress(self, stage: str, pct: float, detail: str = ""):
        if stage in ("提取音频", "加载模型", "读取字幕", "转写中"):
            self.transcribe_bar.setValue(int(pct))
            self.transcribe_bar.setFormat(f"{int(pct)}%")
            if detail:
                self.transcribe_detail.setText(detail)
        elif stage == "翻译":
            self.translate_bar.setValue(int(pct))
            self.translate_bar.setFormat(f"{int(pct)}%")
            if detail:
                self.translate_detail.setText(detail)

    def set_sub_complete(self):
        self.transcribe_bar.setValue(100)
        self.transcribe_bar.setFormat("100%")
        self.translate_bar.setValue(100)
        self.translate_bar.setFormat("100%")

    def set_transcribe_status(self, text: str):
        self.transcribe_label.setText(text)

    def set_translate_status(self, text: str):
        self.translate_label.setText(text)

    def update_eta(self, start_ts: float, pct: float, extra: str = ""):
        elapsed = time.time() - start_ts
        remain, finish = estimate_eta(start_ts, pct / 100)
        parts = [extra] if extra else []
        parts.extend([
            f"已用 {fmt_duration(elapsed)}",
            f"剩余 {remain}",
            f"预计 {finish}",
        ])
        self.detail_label.setText(" | ".join(parts))


# ═══════════════════════════════════════════════════════════════════
# PreviewPanel
# ═══════════════════════════════════════════════════════════════════

class PreviewPanel(QWidget):
    """字幕预览/编辑/查找/偏移面板"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._last_output_dir: Optional[Path] = None
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 0, 0, 0)

        tb = QHBoxLayout()
        tb.addWidget(QLabel("字幕预览"))
        self._find_btn = QPushButton("🔍 查找")
        tb.addWidget(self._find_btn)
        self._save_btn = QPushButton("💾 保存修改")
        tb.addWidget(self._save_btn)
        self._offset_btn = QPushButton("⏱ 偏移")
        self._offset_btn.setToolTip("批量调整字幕时间戳（±秒）")
        tb.addWidget(self._offset_btn)
        tb.addStretch()
        layout.addLayout(tb)

        self.preview = QTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setFont(QFont("Consolas", 10))
        layout.addWidget(self.preview, 1)

    def connect_toolbar(self, find_cb, save_cb, offset_cb):
        self._find_btn.clicked.connect(find_cb)
        self._save_btn.clicked.connect(save_cb)
        self._offset_btn.clicked.connect(offset_cb)

    def set_text(self, text: str):
        self.preview.setReadOnly(False)
        self.preview.setText(text)

    def clear(self):
        self.preview.clear()
        self.preview.setReadOnly(True)

    def append(self, text: str):
        self.preview.append(text)
        sb = self.preview.verticalScrollBar()
        sb.setValue(sb.maximum())

    def get_text(self) -> str:
        return self.preview.toPlainText()

    @property
    def last_output_dir(self) -> Optional[Path]:
        return self._last_output_dir

    @last_output_dir.setter
    def last_output_dir(self, path: Path):
        self._last_output_dir = path


# ═══════════════════════════════════════════════════════════════════
# LogPanel
# ═══════════════════════════════════════════════════════════════════

class LogPanel(QWidget):
    """日志列表面板，支持级别着色、展开 traceback、复制"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._relayouting = False
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.log_list = QListWidget()
        self.log_list.setObjectName("logList")
        self.log_list.setMinimumHeight(60)
        self.log_list.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOn)
        layout.addWidget(self.log_list)

    def add_entry(self, message: str, level: str = "INFO", trace: str = None):
        from datetime import datetime
        ts = datetime.now().strftime("%H:%M:%S")
        text = f"[{ts}] {message}"
        item = QListWidgetItem()
        entry = LogEntry(text, level, trace)
        item.setSizeHint(entry.sizeHint())
        self.log_list.addItem(item)
        self.log_list.setItemWidget(item, entry)
        entry._list_item = item
        QTimer.singleShot(0, lambda: item.setSizeHint(entry.sizeHint()))

    def trim_to(self, max_lines: int):
        while self.log_list.count() > max_lines:
            self.log_list.takeItem(0)

    def count(self) -> int:
        return self.log_list.count()

    def get_all_lines(self) -> List[str]:
        lines = []
        for i in range(self.log_list.count()):
            item = self.log_list.item(i)
            w = self.log_list.itemWidget(item)
            if w is not None and hasattr(w, "message"):
                lines.append(w.message)
                if getattr(w, "trace", None):
                    for tl in w.trace.rstrip().split("\n"):
                        lines.append(f"  {tl}")
            else:
                lines.append(item.text())
        return lines

    def relayout_items(self):
        if self._relayouting:
            return
        self._relayouting = True
        try:
            for i in range(self.log_list.count()):
                it = self.log_list.item(i)
                w = self.log_list.itemWidget(it)
                if w is not None:
                    it.setSizeHint(w.sizeHint())
        finally:
            self._relayouting = False


# ═══════════════════════════════════════════════════════════════════
# SignalBridge
# ═══════════════════════════════════════════════════════════════════

class SignalBridge(QObject):
    """将工作线程的 dict 事件转为 Qt 信号，替代轮询 queue.Queue。

    工作线程调用 bridge.post(event_dict) 来发射事件，
    主线程连接 bridge.event_received 到事件处理槽。
    Qt 信号跨线程自动排队到主线程事件循环，无需手动轮询。
    """
    event_received = Signal(object)

    def post(self, event: dict):
        """工作线程调用（线程安全，Qt 自动排队到主线程）"""
        self.event_received.emit(event)

    def clear(self):
        """兼容旧接口——信号无缓冲，无需清理"""
        pass
