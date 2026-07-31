#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import logging
import queue
import time
import re
from pathlib import Path
from typing import Dict, List, Optional

from PySide6.QtCore import Qt, QObject, Signal, QTimer
from PySide6.QtWidgets import (
    QGroupBox, QVBoxLayout, QHBoxLayout, QLabel, QProgressBar,
    QTextEdit, QListWidget, QListWidgetItem, QWidget, QPushButton,
    QFrame, QSizePolicy, QDialog, QComboBox, QSpinBox,
    QDoubleSpinBox, QLineEdit, QMessageBox,
    QApplication, QAbstractSpinBox, QToolButton,
    QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView,
    QStackedWidget,
)
from PySide6.QtGui import (
    QFont, QFontMetrics, QColor, QBrush, QDragEnterEvent, QDropEvent, QPalette,
)

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
    layout.addLayout(_make_dialog_buttons(dialog))
    result = dialog.exec()
    text = edit.text().strip()
    return text, result == QDialog.Accepted


def _make_dialog_buttons(dialog: QDialog) -> QHBoxLayout:
    """创建确定/取消按钮行"""
    row = QHBoxLayout()
    row.addStretch()
    ok_btn = QPushButton("确定")
    ok_btn.clicked.connect(dialog.accept)
    row.addWidget(ok_btn)
    cancel_btn = QPushButton("取消")
    cancel_btn.clicked.connect(dialog.reject)
    row.addWidget(cancel_btn)
    return row


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
    layout.addLayout(_make_dialog_buttons(dialog))
    result = dialog.exec()
    return spin.value(), result == QDialog.Accepted


class TranslationMonitor(QGroupBox):
    """可折叠翻译监视器：展示批次、句子、速度和最近翻译结果。"""

    def __init__(self, parent=None):
        super().__init__("翻译监视器", parent)
        self._expanded = True
        self._started_at = None
        self._total_sentences = 0
        self._completed_sentences = 0
        self._build_ui()

    def _build_ui(self):
        self.setCheckable(True)
        self.setChecked(True)
        self.toggled.connect(self._toggle_content)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(6)

        self.summary = QLabel("等待翻译任务…")
        self.summary.setObjectName("translationMonitorSummary")
        layout.addWidget(self.summary)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setFormat("0%")
        self.progress.setFixedHeight(17)
        layout.addWidget(self.progress)

        self.stats = QLabel("批次 --/--  ·  句子 --/--  ·  速度 --  ·  剩余 --")
        self.stats.setObjectName("translationMonitorStats")
        layout.addWidget(self.stats)

        self.recent = QListWidget()
        self.recent.setObjectName("translationRecentList")
        self.recent.setMaximumHeight(112)
        self.recent.setFocusPolicy(Qt.NoFocus)
        layout.addWidget(self.recent)
        self._content_widgets = [self.summary, self.progress, self.stats, self.recent]

    def _toggle_content(self, checked):
        for widget in self._content_widgets:
            widget.setVisible(checked)
        self._expanded = checked

    def reset(self):
        self._started_at = None
        self._total_sentences = 0
        self._completed_sentences = 0
        self.summary.setText("等待翻译任务…")
        self.progress.setValue(0)
        self.progress.setFormat("0%")
        self.stats.setText("批次 --/--  ·  句子 --/--  ·  速度 --  ·  剩余 --")
        self.recent.clear()

    def update_progress(self, event):
        import time
        if self._started_at is None:
            self._started_at = time.monotonic()
        detail = event.get("detail", "")
        batch_id = event.get("batch_id")
        total_batches = event.get("total_batches")
        if batch_id is None or total_batches is None:
            batch = re.search(r"批次\s+(\d+)/(\d+)", detail)
            batch_id = int(batch.group(1)) if batch else None
            total_batches = int(batch.group(2)) if batch else None
        else:
            batch = True
        self._total_sentences = max(self._total_sentences, int(event.get("total_sentences") or 0))
        self._completed_sentences = max(self._completed_sentences, int(event.get("completed_sentences") or 0))
        pct = max(0, min(100, int(event.get("percent", 0))))
        self.progress.setValue(pct)
        self.progress.setFormat(f"{pct}%")
        self.summary.setText(detail or "正在请求大模型…")
        batch_text = f"批次 {batch_id}/{total_batches}" if batch_id and total_batches else "批次 --/--"
        elapsed = max(time.monotonic() - self._started_at, 0.1)
        speed = self._completed_sentences / elapsed * 60 if self._completed_sentences else 0
        speed_text = f"{speed:.1f} 批/分钟" if speed else "计算中"
        self.stats.setText(
            f"{batch_text}  ·  句子约 {self._completed_sentences}/{self._total_sentences or '--'}  ·  "
            f"速度 {speed_text}  ·  剩余 {max(0, 100 - pct)}%")
        if detail and (not self.recent.count() or self.recent.item(0).text() != detail):
            self.recent.insertItem(0, detail)
            while self.recent.count() > 10:
                self.recent.takeItem(10)

    def finish(self):
        self.progress.setValue(100)
        self.progress.setFormat("100%")
        self.summary.setText("翻译完成")


class TranslationMonitorDialog(QDialog):
    """翻译监视器二级页面。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("翻译进度详情")
        self.setMinimumSize(520, 360)
        self.resize(620, 420)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        self.monitor = TranslationMonitor(self)
        # 二级页面始终展开，避免用户打开后还要再次点击标题。
        self.monitor.setChecked(True)
        layout.addWidget(self.monitor)
        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(self.hide)
        row = QHBoxLayout()
        row.addStretch()
        row.addWidget(close_btn)
        layout.addLayout(row)


class ProgressPanel(QFrame):
    def __init__(self, parent=None, details_cb=None):
        super().__init__(parent)
        self._details_cb = details_cb
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
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(6)
        header = QHBoxLayout()
        title = QLabel("进度")
        title.setStyleSheet("font-weight:600; font-size:12px; padding:2px 0;")
        title.setFixedHeight(20)
        header.addWidget(title)
        self.details_btn = QToolButton()
        self.details_btn.setText("详情")
        self.details_btn.setToolTip("打开翻译监视器")
        self.details_btn.setAutoRaise(True)
        self.details_btn.clicked.connect(self._open_details)
        header.addWidget(self.details_btn)
        header.addStretch()
        layout.addLayout(header)
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
    def _open_details(self):
        if self._details_cb:
            self._details_cb()

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
        if stage in ("提取音频", "加载模型", "读取字幕", "转写中", "转写完成"):
            bar_pct = 100 if stage == "转写完成" else int(pct)
            self.transcribe_bar.setValue(bar_pct)
            self.transcribe_bar.setFormat(f"{bar_pct}%")
            if detail:
                self.transcribe_detail.setText(detail)
        elif stage == "翻译":
            if self.transcribe_bar.value() < 100:
                self.transcribe_bar.setValue(100)
                self.transcribe_bar.setFormat("100%")
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


class PreviewPanel(QFrame):
    """字幕预览面板，支持拖入 .srt 文件"""

    fileDropped = Signal(str)  # 拖入文件路径

    def __init__(self, parent=None):
        super().__init__(parent)
        self._last_output_dir: Optional[Path] = None
        self._save_cb = None
        self._offset_cb = None
        self._raw_text = ""
        self._updating = False
        self._highlighted_rows: set = set()
        self._build_ui()

    def _palette_colors(self) -> dict:
        """根据当前主题返回表格配色（跟随应用明暗主题）"""
        dark = self.palette().color(QPalette.Base).lightness() < 128
        if dark:
            return {
                "index": "#64748b", "time": "#94a3b8",
                "text": "#e2e8f0", "translation": "#a5b4fc",
                "highlight_bg": "#fde68a", "highlight_fg": "#1e293b",
            }
        return {
            "index": "#94a3b8", "time": "#64748b",
            "text": "#0f172a", "translation": "#6d28d9",
            "highlight_bg": "#fde68a", "highlight_fg": "#1e293b",
        }

    def _style_item(self, item: QTableWidgetItem, kind: str):
        """按列类型设置单元格字体与颜色（kind: index/time/text/translation）"""
        c = self._palette_colors()
        if kind == "index":
            item.setFont(QFont("Consolas", 8))
            item.setForeground(QBrush(QColor(c["index"])))
            item.setTextAlignment(Qt.AlignCenter)
        elif kind == "time":
            item.setFont(QFont("Consolas", 8))
            item.setForeground(QBrush(QColor(c["time"])))
        elif kind == "text":
            item.setFont(QFont("Consolas", 9))
            item.setForeground(QBrush(QColor(c["text"])))
        else:  # translation
            item.setFont(QFont("Consolas", 9))
            item.setForeground(QBrush(QColor(c["translation"])))

    def _render_structured_preview(self):
        """将 SRT 文本渲染为紧凑表格：序号 / 时间轴 / 原文 / 译文。

        原始文本始终保留在 _raw_text 中，供编辑与保存使用；表格只负责展示。
        """
        blocks = [b.strip() for b in self._raw_text.replace("\r\n", "\n").split("\n\n") if b.strip()]
        rows = []
        for block in blocks:
            lines = block.splitlines()
            if len(lines) < 3:
                continue
            timeline = lines[1].strip()
            text_lines = [line.strip() for line in lines[2:] if line.strip()]
            if not text_lines:
                continue
            original = text_lines[0]
            translated = "\n".join(text_lines[1:]) or "—"
            rows.append((timeline, original, translated))
        if not rows:
            self._highlighted_rows.clear()
            self._stack.setCurrentWidget(self._empty_label)
            return
        self._stack.setCurrentWidget(self.preview)
        self._updating = True
        try:
            self._highlighted_rows.clear()
            self.preview.setRowCount(0)
            self.preview.setRowCount(len(rows))
            for r, (timeline, original, translated) in enumerate(rows):
                for col, (kind, text) in enumerate(
                    (("index", str(r + 1)), ("time", timeline), ("text", original), ("translation", translated))
                ):
                    item = QTableWidgetItem(text)
                    self._style_item(item, kind)
                    if kind == "index":
                        # 序号列始终不可编辑；其余列由 setReadOnly 统一控制
                        item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                    self.preview.setItem(r, col, item)
        finally:
            self._updating = False

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(4)
        tb = QHBoxLayout()
        title = QLabel("字幕预览")
        title.setStyleSheet("font-weight:600; font-size:12px; padding:2px 0;")
        tb.addWidget(title)
        self._edit_btn = QPushButton("✏ 编辑")
        self._edit_btn.clicked.connect(self._open_edit_dialog)
        tb.addWidget(self._edit_btn)
        tb.addStretch()
        layout.addLayout(tb)

        self.preview = QTableWidget(0, 4)
        self.preview.setObjectName("subtitlePreview")
        self.preview.setHorizontalHeaderLabels(["#", "时间轴", "原文", "译文"])
        self.preview.setShowGrid(False)
        self.preview.setAlternatingRowColors(True)
        self.preview.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.preview.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.preview.setSelectionMode(QAbstractItemView.SingleSelection)
        self.preview.setWordWrap(True)
        self.preview.setMouseTracking(True)
        self.preview.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.preview.verticalHeader().setVisible(False)
        # 行高按内容自适应（多行译文完整显示），同时保留最小行高
        self.preview.verticalHeader().setMinimumSectionSize(28)
        self.preview.verticalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        header = self.preview.horizontalHeader()
        header.setHighlightSections(False)
        header.setSectionResizeMode(0, QHeaderView.Fixed)
        header.setSectionResizeMode(1, QHeaderView.Fixed)
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        header.setSectionResizeMode(3, QHeaderView.Stretch)
        self.preview.setColumnWidth(0, 40)
        self.preview.setColumnWidth(1, 185)
        self.preview.itemChanged.connect(self._on_item_changed)

        self._empty_label = QLabel(
            "\n\n\n\n                 暂无字幕\n\n                 添加或拖入 .srt 文件后，字幕会显示在这里"
        )
        self._empty_label.setAlignment(Qt.AlignCenter)
        self._empty_label.setStyleSheet("color:#64748b; background:transparent; font-size:13px;")
        self._stack = QStackedWidget()
        self._stack.addWidget(self._empty_label)
        self._stack.addWidget(self.preview)
        layout.addWidget(self._stack, 1)

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
        self._raw_text = text
        self.setReadOnly(True)
        self._render_structured_preview()

    def clear(self):
        self._raw_text = ""
        self._highlighted_rows.clear()
        self._updating = True
        try:
            self.preview.setRowCount(0)
        finally:
            self._updating = False
        self.setReadOnly(True)
        self._stack.setCurrentWidget(self._empty_label)

    def append(self, text: str):
        self._raw_text = f"{self._raw_text}\n\n{text}".strip()
        self._render_structured_preview()
        self.preview.scrollToBottom()

    def get_text(self) -> str:
        return self._raw_text

    def setReadOnly(self, readonly: bool):
        """控制表格是否允许直接编辑（完成后放开，供微调译文/时间轴）"""
        if readonly:
            self.preview.setEditTriggers(QAbstractItemView.NoEditTriggers)
        else:
            self.preview.setEditTriggers(
                QAbstractItemView.DoubleClicked
                | QAbstractItemView.SelectedClicked
                | QAbstractItemView.EditKeyPressed
            )

    # ── 查找高亮 ──

    def highlight_rows(self, rows):
        """高亮命中的行，并滚动到第一个命中行"""
        self.clear_highlight()
        c = self._palette_colors()
        for r in rows:
            for col in range(self.preview.columnCount()):
                item = self.preview.item(r, col)
                if item:
                    item.setBackground(QBrush(QColor(c["highlight_bg"])))
                    item.setForeground(QBrush(QColor(c["highlight_fg"])))
        self._highlighted_rows = set(rows)
        if rows:
            self.preview.scrollToItem(self.preview.item(rows[0], 0), QAbstractItemView.PositionAtTop)

    def clear_highlight(self):
        """恢复高亮行的默认样式（交替背景 + 各列颜色）"""
        kinds = ["index", "time", "text", "translation"]
        for r in self._highlighted_rows:
            for col in range(self.preview.columnCount()):
                item = self.preview.item(r, col)
                if item:
                    item.setBackground(QBrush())
                    self._style_item(item, kinds[col])
        self._highlighted_rows.clear()

    # ── 单元格编辑回写 _raw_text ──

    def _on_item_changed(self, item: QTableWidgetItem):
        """用户直接在表格里改时间轴/原文/译文后，同步回 _raw_text（保存走 get_text）"""
        if getattr(self, "_updating", False):
            return
        if not self._raw_text.strip():
            return
        col = item.column()
        if col not in (1, 2, 3):
            return
        row = item.row()
        blocks = [b.strip() for b in self._raw_text.replace("\r\n", "\n").split("\n\n") if b.strip()]
        if row >= len(blocks):
            return
        lines = blocks[row].splitlines()
        if col == 1:
            # 时间轴
            if len(lines) < 2:
                return
            lines[1] = item.text().strip()
        else:
            text_lines = [l.strip() for l in lines[2:] if l.strip()]
            if not text_lines:
                return
            if col == 2:
                # 原文（保持第 1 行语义；译文原样保留）
                text_lines[0] = item.text()
                lines = lines[:2] + text_lines
            else:
                # 译文（可多行；清空或 “—” 视为无译文）
                new_trans = item.text()
                if new_trans.strip() in ("", "—"):
                    lines = lines[:2] + [text_lines[0]]
                else:
                    lines = lines[:2] + [text_lines[0]] + new_trans.split("\n")
        blocks[row] = "\n".join(lines)
        body = "\n\n".join(blocks)
        # 保留原始文本首尾空白，避免重建丢失 SRT 结尾换行等格式
        leading = self._raw_text[: len(self._raw_text) - len(self._raw_text.lstrip())]
        trailing = self._raw_text[len(self._raw_text.rstrip()):]
        self._raw_text = leading + body + trailing

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


class LogPanel(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._relayouting = False
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(4)
        title = QLabel("日志")
        title.setStyleSheet("font-weight:600; font-size:12px; padding:2px 0;")
        title.setFixedHeight(20)
        layout.addWidget(title)
        self.log_list = QListWidget()
        self.log_list.setObjectName("logList")
        self.log_list.setMinimumHeight(60)
        self.log_list.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOn)
        self.log_list.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
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
