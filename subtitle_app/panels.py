#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import logging
import queue
import time
from pathlib import Path
from typing import Dict, List, Optional

from PySide6.QtCore import Qt, QObject, Signal, QTimer
from PySide6.QtWidgets import (
    QGroupBox, QVBoxLayout, QHBoxLayout, QLabel, QProgressBar,
    QTextEdit, QListWidget, QListWidgetItem, QWidget, QPushButton,
    QFrame, QSizePolicy, QDialog, QComboBox, QSpinBox,
    QDoubleSpinBox, QLineEdit, QMessageBox,
    QApplication, QAbstractSpinBox,
)
from PySide6.QtGui import QFont, QFontMetrics, QColor, QDragEnterEvent, QDropEvent

from .srt_utils import fmt_duration, estimate_eta
from .widgets import LogEntry

logger = logging.getLogger(__name__)


def _silent_text_input(parent, title: str, label: str) -> tuple:
    """无声音的文本输入对话框"""
    dialog = QDialog(parent)
    dialog.setWindowTitle(title)
    layout = QVBoxLayout(dialog)
    layout.addWidget(QLabel(label))
    edit = QLineEdit()
    layout.addWidget(edit)
    btn_row = QHBoxLayout()
    btn_row.addStretch()
    ok_btn = QPushButton("确定")
    ok_btn.clicked.connect(dialog.accept)
    btn_row.addWidget(ok_btn)
    cancel_btn = QPushButton("取消")
    cancel_btn.clicked.connect(dialog.reject)
    btn_row.addWidget(cancel_btn)
    layout.addLayout(btn_row)
    result = dialog.exec()
    text = edit.text().strip()
    return text, result == QDialog.Accepted


def _silent_double_input(parent, title: str, label: str,
                         default: float = 0, min_v: float = -3600,
                         max_v: float = 3600, decimals: int = 1) -> tuple:
    """无声音的数值输入对话框"""
    dialog = QDialog(parent)
    dialog.setWindowTitle(title)
    layout = QVBoxLayout(dialog)
    layout.addWidget(QLabel(label))
    spin = QDoubleSpinBox()
    spin.setRange(min_v, max_v)
    spin.setValue(default)
    spin.setDecimals(decimals)
    spin.setFixedWidth(120)
    layout.addWidget(spin)
    btn_row = QHBoxLayout()
    btn_row.addStretch()
    ok_btn = QPushButton("确定")
    ok_btn.clicked.connect(dialog.accept)
    btn_row.addWidget(ok_btn)
    cancel_btn = QPushButton("取消")
    cancel_btn.clicked.connect(dialog.reject)
    btn_row.addWidget(cancel_btn)
    layout.addLayout(btn_row)
    result = dialog.exec()
    return spin.value(), result == QDialog.Accepted


class ProgressPanel(QGroupBox):
    def __init__(self, parent=None):
        super().__init__("进度", parent)
        self._start_time: Optional[float] = None
        self._build_ui()

    def _build_sub_group(self, name: str) -> tuple:
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

        top = QHBoxLayout()
        self.lang_label = QLabel("语言：auto")
        top.addWidget(self.lang_label)
        top.addStretch()
        self.counter_label = QLabel("已转写 0/0 | 已翻译 0/0 | 缓存 0")
        top.addWidget(self.counter_label)
        layout.addLayout(top)

        dual = QVBoxLayout()
        self._transcribe_group, self.transcribe_label, self.transcribe_bar, self.transcribe_detail = \
            self._build_sub_group("转写")
        self._translate_group, self.translate_label, self.translate_bar, self.translate_detail = \
            self._build_sub_group("翻译")
        dual.addWidget(self._transcribe_group, 1)
        dual.addWidget(self._translate_group, 1)
        layout.addLayout(dual)

        bot = QHBoxLayout()
        self.detail_label = QLabel("已用 --:-- | 剩余 --:-- | 预计 --")
        bot.addWidget(self.detail_label, 1)
        layout.addLayout(bot)

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
        self.counter_label.setText(f"已转写 {generated}/{total} | 已翻译 {translated}/{total} | 缓存 {cache}")

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
        parts.extend([f"已用 {fmt_duration(elapsed)}", f"剩余 {remain}", f"预计 {finish}"])
        self.detail_label.setText(" | ".join(parts))


class PreviewPanel(QWidget):
    """字幕预览面板，支持拖入 .srt 文件"""

    fileDropped = Signal(str)  # 拖入文件路径

    def __init__(self, parent=None):
        super().__init__(parent)
        self._last_output_dir: Optional[Path] = None
        self._save_cb = None
        self._offset_cb = None
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 0, 0, 0)

        tb = QHBoxLayout()
        title = QLabel("字幕预览")
        title.setStyleSheet("font-weight:600; font-size:12px; padding:2px 0;")
        tb.addWidget(title)
        self._edit_btn = QPushButton("✏ 编辑")
        self._edit_btn.clicked.connect(self._open_edit_dialog)
        tb.addWidget(self._edit_btn)
        tb.addStretch()
        layout.addLayout(tb)

        self.preview = QTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setFont(QFont("Consolas", 10))
        self.preview.setAcceptDrops(False)
        layout.addWidget(self.preview, 1)

        self.setAcceptDrops(True)

    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls():
            for url in event.mimeData().urls():
                if url.toLocalFile().lower().endswith(".srt"):
                    event.acceptProposedAction()
                    return
        event.ignore()

    def dropEvent(self, event: QDropEvent):
        for url in event.mimeData().urls():
            path = url.toLocalFile()
            if path.lower().endswith(".srt"):
                try:
                    text = Path(path).read_text(encoding="utf-8-sig")
                    self.set_text(text)
                    self._last_output_dir = Path(path).parent
                    self.fileDropped.emit(path)
                except Exception as e:
                    logger.error(f"读取字幕文件失败: {e}")
                    QMessageBox.warning(self, "错误", f"读取字幕文件失败:\n{e}")
                break

    def connect_toolbar(self, find_cb, save_cb, offset_cb):
        self._save_cb = save_cb
        self._offset_cb = offset_cb

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

    def _open_edit_dialog(self):
        content = self.get_text().strip()
        if not content:
            QMessageBox.information(self, "提示", "暂无字幕可编辑")
            return
        dlg = EditDialog(content, self, save_cb=self._save_cb, offset_cb=self._offset_cb)
        if dlg.exec() == QDialog.Accepted:
            merged = dlg.get_merged_text()
            self.set_text(merged)
            if dlg._save_requested and self._save_cb:
                self._save_cb()

    @property
    def last_output_dir(self) -> Optional[Path]:
        return self._last_output_dir

    @last_output_dir.setter
    def last_output_dir(self, path: Path):
        self._last_output_dir = path


class EditDialog(QDialog):
    """分页字幕编辑弹窗（按字幕段分页）"""

    def __init__(self, full_text: str, parent=None, save_cb=None, offset_cb=None):
        super().__init__(parent)
        self._full_text = full_text
        self._blocks = [b.strip() for b in full_text.split("\n\n") if b.strip()]
        self._page_size = 10
        self._current_page = 0
        self._total_pages = 0
        self._page_edits: Dict[int, str] = {}
        self._save_cb = save_cb
        self._offset_cb = offset_cb
        self._save_requested = False
        self.setWindowTitle("编辑字幕")
        self.setMinimumSize(560, 450)
        self.resize(700, 550)
        self._build_ui()
        self._rebuild_pages()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        nav = QHBoxLayout()
        nav.addWidget(QLabel("每页段数："))
        self._page_size_combo = QComboBox()
        self._page_size_combo.addItems(["10", "20", "50", "全部"])
        self._page_size_combo.setCurrentText("10")
        self._page_size_combo.currentTextChanged.connect(self._on_page_size_changed)
        nav.addWidget(self._page_size_combo)

        nav.addSpacing(12)
        nav.addWidget(QLabel("跳转："))
        self._page_input = QSpinBox()
        self._page_input.setMinimum(1)
        self._page_input.setFixedWidth(60)
        self._page_input.setButtonSymbols(QAbstractSpinBox.NoButtons)
        self._page_input.valueChanged.connect(self._go_to_page)
        nav.addWidget(self._page_input)

        self._prev_btn = QPushButton("◀")
        self._prev_btn.setFixedWidth(32)
        self._prev_btn.clicked.connect(self._prev_page)
        nav.addWidget(self._prev_btn)

        self._page_label = QLabel("0/0")
        nav.addWidget(self._page_label)

        self._next_btn = QPushButton("▶")
        self._next_btn.setFixedWidth(32)
        self._next_btn.clicked.connect(self._next_page)
        nav.addWidget(self._next_btn)

        nav.addStretch()

        self._dirty_label = QLabel("")
        self._dirty_label.setStyleSheet("color:#ef4444; font-size:11px;")
        nav.addWidget(self._dirty_label)

        layout.addLayout(nav)

        action_row = QHBoxLayout()
        self._find_btn = QPushButton("🔍 查找")
        self._find_btn.clicked.connect(self._find_in_editor)
        action_row.addWidget(self._find_btn)
        self._save_all_btn = QPushButton("💾 保存")
        self._save_all_btn.setObjectName("startBtn")
        self._save_all_btn.clicked.connect(self._save_all_and_exit)
        action_row.addWidget(self._save_all_btn)
        self._offset_btn = QPushButton("⏱ 偏移")
        self._offset_btn.setToolTip("批量调整字幕时间戳（±秒）")
        self._offset_btn.clicked.connect(self._offset_time)
        action_row.addWidget(self._offset_btn)
        action_row.addStretch()
        layout.addLayout(action_row)

        self._editor = QTextEdit()
        self._editor.setFont(QFont("Consolas", 10))
        layout.addWidget(self._editor, 1)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        self._close_btn = QPushButton("取消")
        self._close_btn.clicked.connect(self.reject)
        btn_row.addWidget(self._close_btn)
        layout.addLayout(btn_row)

    def _page_block_range(self, page: int) -> tuple:
        if self._page_size <= 0:
            return 0, len(self._blocks)
        start = page * self._page_size
        end = min(start + self._page_size, len(self._blocks))
        return start, end

    def _rebuild_pages(self):
        total = len(self._blocks)
        if self._page_size <= 0:
            self._total_pages = 1
        else:
            self._total_pages = max(1, (total + self._page_size - 1) // self._page_size)
        self._current_page = min(self._current_page, self._total_pages - 1)
        self._current_page = max(0, self._current_page)
        self._show_page()

    def _show_page(self):
        start, end = self._page_block_range(self._current_page)
        if self._current_page in self._page_edits:
            text = self._page_edits[self._current_page]
        else:
            text = "\n\n".join(self._blocks[start:end])
        self._editor.setText(text)

        total_str = "全部" if self._page_size <= 0 else str(self._total_pages)
        self._page_label.setText(f"第 {self._current_page + 1}/{total_str} 页")
        self._page_input.blockSignals(True)
        self._page_input.setMinimum(1)
        self._page_input.setMaximum(max(1, self._total_pages))
        self._page_input.setValue(self._current_page + 1)
        self._page_input.blockSignals(False)

        dirty_count = len(self._page_edits)
        self._dirty_label.setText(f"⚠ {dirty_count} 页未保存" if dirty_count else "")
        self._update_nav()

    def _on_page_size_changed(self, text: str):
        self._page_size = 0 if text == "全部" else int(text)
        self._current_page = 0
        self._rebuild_pages()

    def _update_nav(self):
        self._prev_btn.setEnabled(self._current_page > 0)
        self._next_btn.setEnabled(self._current_page < self._total_pages - 1)

    def _go_to_page(self, page: int):
        target = page - 1
        if 0 <= target < self._total_pages and target != self._current_page:
            self._save_edit_buffer()
            self._current_page = target
            self._show_page()

    def _prev_page(self):
        if self._current_page > 0:
            self._save_edit_buffer()
            self._current_page -= 1
            self._show_page()
            self._update_nav()

    def _next_page(self):
        if self._current_page < self._total_pages - 1:
            self._save_edit_buffer()
            self._current_page += 1
            self._show_page()
            self._update_nav()

    def _save_edit_buffer(self):
        text = self._editor.toPlainText().strip()
        start, end = self._page_block_range(self._current_page)
        original = "\n\n".join(self._blocks[start:end])
        if text != original:
            self._page_edits[self._current_page] = text
        elif self._current_page in self._page_edits:
            del self._page_edits[self._current_page]

    def _save_all_and_exit(self):
        self._save_edit_buffer()
        self._save_requested = True
        self.accept()

    def _find_in_editor(self):
        text, ok = _silent_text_input(self, "查找", "输入要查找的文本：")
        if not ok or not text:
            return
        editor = self._editor
        fmt_hl = editor.currentCharFormat()
        cursor = editor.textCursor()
        cursor.select(cursor.SelectionType.Document)
        cursor.setCharFormat(fmt_hl)
        fmt = QFont()
        fmt.setBold(True)
        fmt.setBackground(QColor("#fef08a"))
        cursor = editor.textCursor()
        cursor.movePosition(cursor.MoveOperation.Start)
        pos = 0
        content = editor.toPlainText()
        found = False
        while True:
            idx = content.find(text, pos)
            if idx == -1:
                break
            found = True
            cursor.setPosition(idx)
            cursor.setPosition(idx + len(text), cursor.MoveMode.KeepAnchor)
            cursor.setCharFormat(fmt)
            pos = idx + len(text)
        if not found:
            QMessageBox.information(self, "查找", f"未找到：{text}")

    def _offset_time(self):
        self._save_edit_buffer()
        offset, ok = _silent_double_input(self, "时间偏移",
                                           "偏移量（秒）：正数=延后，负数=提前")
        if not ok:
            return
        import re
        from .srt_utils import srt_time_to_seconds, seconds_to_srt_time
        ts_re = re.compile(r"(\d+:\d{1,2}:\d{1,2}[,.]\d{1,3})\s*-->\s*(\d+:\d{1,2}:\d{1,2}[,.]\d{1,3})")

        def _shift(m):
            start = max(0, srt_time_to_seconds(m.group(1)) + offset)
            end = max(0, srt_time_to_seconds(m.group(2)) + offset)
            return f"{seconds_to_srt_time(start)} --> {seconds_to_srt_time(end)}"

        merged = self.get_merged_text()
        merged = ts_re.sub(_shift, merged)
        self._full_text = merged
        self._blocks = [b.strip() for b in merged.split("\n\n") if b.strip()]
        self._page_edits.clear()
        self._current_page = 0
        self._rebuild_pages()
        self._save_requested = True

    def get_merged_text(self) -> str:
        parts = []
        for page_idx in range(self._total_pages):
            if page_idx in self._page_edits:
                parts.append(self._page_edits[page_idx])
            else:
                start, end = self._page_block_range(page_idx)
                parts.append("\n\n".join(self._blocks[start:end]))
        return "\n\n".join(parts)

    def _add_log_message(self, msg: str):
        from datetime import datetime
        logger.info(msg)


class LogPanel(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._relayouting = False
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        title = QLabel("日志")
        title.setStyleSheet("font-weight:600; font-size:12px; padding:2px 0;")
        layout.addWidget(title)
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
            item = self.log_list.takeItem(0)
            if item:
                widget = self.log_list.itemWidget(item)
                if widget:
                    widget.deleteLater()
                del item

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


class SignalBridge(QObject):
    event_received = Signal(object)

    def post(self, event: dict):
        self.event_received.emit(event)

    def clear(self):
        pass
