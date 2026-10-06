#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import logging
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from PySide6.QtCore import Qt, QObject, Signal, QTimer
from PySide6.QtWidgets import (
    QGroupBox, QVBoxLayout, QHBoxLayout, QLabel, QProgressBar,
    QTextEdit, QListWidget, QListWidgetItem,
    QFrame, QSizePolicy, QDialog, QComboBox, QSpinBox,
    QDoubleSpinBox, QLineEdit, QMessageBox,
    QAbstractSpinBox,
    QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView,
    QStackedWidget, QApplication,
)
from PySide6.QtGui import (
    QFont, QColor, QBrush, QDragEnterEvent, QDropEvent, QPalette,
)

from .srt_utils import _read_text_auto, split_srt_blocks, SRT_TIMING_LINE_RE, PREVIEW_BLOCK_LIMIT
from .widgets import LogEntry
from .icons import action_button, icon_data_url
from .theme import load_theme_colors, _mix_hex

logger = logging.getLogger(__name__)

# 只显示最开始的 100 块；实时填满后冻结，全文仍保留供编辑与保存。
MAX_LIVE_PREVIEW_BLOCKS = PREVIEW_BLOCK_LIMIT


def _visible_block_slice(blocks: List[str],
                         max_blocks: int = MAX_LIVE_PREVIEW_BLOCKS) -> Tuple[List[str], int]:
    """返回全文最开始的 max_blocks 块，源块索引从 0 开始。"""
    return blocks[:max_blocks], 0


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
    ok_btn = action_button("确定")
    ok_btn.setObjectName("confirmBtn")
    ok_btn.clicked.connect(dialog.accept)
    row.addWidget(ok_btn)
    cancel_btn = action_button("取消")
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


class ProgressPanel(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
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
        title.setObjectName("panelTitle")
        title.setFixedHeight(20)
        header.addWidget(title)
        header.addStretch()
        layout.addLayout(header)

        top = QHBoxLayout()
        self.lang_label = QLabel("语言：auto")
        top.addWidget(self.lang_label)
        top.addStretch()
        self.counter_label = QLabel("已转写 0/0 | 已翻译 0/0 | 缓存 0")
        top.addWidget(self.counter_label)
        layout.addLayout(top)

        # 上下两行：上行两阶段串行（先全部转写、后顺序翻译）共用的阶段进度条，
        # 下行总进度条；两行结构一致、等宽对齐
        self._stage_group, self.stage_label, self.stage_bar, self.stage_detail = \
            self._build_sub_group("处理")
        layout.addWidget(self._stage_group)
        self._overall_group, self.overall_label, self.overall_progress, self.overall_detail = \
            self._build_sub_group("总进度")
        self.overall_label.setStyleSheet("font-weight:600; color:#6366f1;")
        self.overall_progress.setFormat("%p%")
        layout.addWidget(self._overall_group)

        bot = QHBoxLayout()
        self.detail_label = QLabel("已用 --:-- | 剩余 --:-- | 预计 --")
        bot.addWidget(self.detail_label, 1)
        layout.addLayout(bot)

    def reset(self):
        self.overall_progress.setValue(0)
        self.overall_label.setText("等待中")
        self.overall_detail.setText("")
        self.stage_bar.setValue(0)
        self.stage_bar.setFormat("")
        self.stage_label.setText("等待中")
        self.stage_detail.setText("")
        self.detail_label.setText("")
        self.lang_label.setText("语言：auto")
        self.counter_label.setText("已转写 0/0 | 已翻译 0/0 | 缓存 0")


class PreviewPanel(QFrame):
    """字幕预览面板，支持拖入 .srt 文件"""

    fileDropped = Signal(str)  # 拖入文件路径

    def __init__(self, parent=None):
        super().__init__(parent)
        self._last_output_dir: Optional[Path] = None
        # 当前预览内容来源的字幕文件：保存时回写该文件，避免按当前选中项
        # 推导目标导致"预览 A 却覆盖 B"。实时预览等无文件来源时为 None。
        self._source_path: Optional[Path] = None
        self._save_cb = None
        self._raw_text = ""
        self._preview_blocks: List[str] = []
        self._frozen_preview_blocks: Optional[List[str]] = None
        self._preview_stale = False
        self._updating = False
        self._highlighted_rows: set = set()
        self._build_ui()

    def _palette_colors(self) -> dict:
        """根据当前主题返回表格配色（跟随应用明暗主题）"""
        dark = self.palette().color(QPalette.Base).lightness() < 128
        owner = self
        colors = None
        while owner is not None:
            candidate = getattr(owner, "colors", None)
            if isinstance(candidate, dict):
                colors = candidate
                break
            owner = owner.parentWidget()
        if colors is None:
            light, dark_colors = load_theme_colors()
            colors = dark_colors if dark else light
        return {
            "index": _mix_hex(colors["text_sec"], colors["card"], 0.20),
            "time": colors["text_sec"], "text": colors["text"],
            "translation": _mix_hex(colors["accent"], colors["text"], 0.20),
            "empty": colors["text_muted"],
            "highlight_bg": "#fde68a", "highlight_fg": "#1e293b",
        }

    def _style_item(self, item: QTableWidgetItem, kind: str, colors: Optional[dict] = None):
        """按列类型设置单元格字体、颜色与对齐（kind: index/time/text/translation）"""
        c = colors if colors is not None else self._palette_colors()
        if kind == "index":
            item.setFont(QFont("Consolas", 8))
            item.setForeground(QBrush(QColor(c["index"])))
            item.setTextAlignment(Qt.AlignCenter | Qt.AlignVCenter)
        elif kind == "time":
            item.setFont(QFont("Consolas", 8))
            item.setForeground(QBrush(QColor(c["time"])))
            item.setTextAlignment(Qt.AlignCenter | Qt.AlignVCenter)
        elif kind == "text":
            font = QFont(QApplication.font())
            font.setPointSize(9)
            item.setFont(font)
            item.setForeground(QBrush(QColor(c["text"])))
            item.setTextAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        else:  # translation
            font = QFont(QApplication.font())
            font.setPointSize(9)
            item.setFont(font)
            item.setForeground(QBrush(QColor(c["empty"] if item.text() == "—" else c["translation"])))
            item.setTextAlignment(Qt.AlignLeft | Qt.AlignVCenter)

    def refresh_theme(self):
        """主题切换后重渲染表格，使单元格颜色跟随新主题。

        QSS 切换不会重设 QTableWidgetItem 的 QBrush 前景/背景色，
        不重渲染的话旧主题的文字颜色会残留在新主题背景上（原文列近乎隐形）。
        """
        highlighted_rows = sorted(self._highlighted_rows)
        if self._raw_text.strip():
            self._render_structured_preview()
        if highlighted_rows:
            self.highlight_rows(
                [r for r in highlighted_rows if r < self.preview.rowCount()])

    def _render_structured_preview(self):
        """将 SRT 文本渲染为紧凑表格：序号 / 时间轴 / 原文 / 译文。

        原始文本始终保留在 _raw_text 中，供编辑与保存使用；表格只负责展示
        （最多渲染最开始的 MAX_LIVE_PREVIEW_BLOCKS 块）。
        """
        display_blocks = self._frozen_preview_blocks if self._frozen_preview_blocks is not None else self._preview_blocks
        blocks, offset = _visible_block_slice(display_blocks)
        rows = []
        for block_idx, block in enumerate(blocks):
            lines = block.splitlines()
            timing_index = 0 if lines and SRT_TIMING_LINE_RE.match(lines[0]) else 1
            content_start = timing_index + 1
            if len(lines) <= content_start or not SRT_TIMING_LINE_RE.match(lines[timing_index]):
                continue
            timeline = lines[timing_index].strip()
            text_lines = lines[content_start:]
            if not any(line.strip() for line in text_lines):
                continue
            original = text_lines[0].strip()
            translated = "\n".join(text_lines[1:]) or "—"
            # 全文块索引随单元格存入 UserRole：编辑回写按它定位源块。
            # 渲染会跳过无效块（行数不足/无文本），表格行号 ≠ 块序号，
            # 若按行号回写会写错块或静默丢失编辑。
            rows.append((offset + block_idx, timeline, original, translated))
        if not rows:
            self._highlighted_rows.clear()
            self.preview.setRowCount(0)
            self._stack.setCurrentWidget(self._empty_label)
            return
        self._stack.setCurrentWidget(self.preview)
        colors = self._palette_colors()
        signals_blocked = self.preview.blockSignals(True)
        updates_enabled = self.preview.updatesEnabled()
        self.preview.setUpdatesEnabled(False)
        self._updating = True
        try:
            self._highlighted_rows.clear()
            self.preview.setRowCount(len(rows))
            for r, (block_idx, timeline, original, translated) in enumerate(rows):
                for col, (kind, text) in enumerate(
                    (("index", str(r + 1)), ("time", timeline), ("text", original), ("translation", translated))
                ):
                    item = self.preview.item(r, col)
                    if item is None:
                        item = QTableWidgetItem()
                        self.preview.setItem(r, col, item)
                    item.setText(text)
                    item.setData(Qt.UserRole, block_idx)
                    item.setBackground(QBrush())
                    self._style_item(item, kind, colors)
                    if kind == "index":
                        # 序号列始终不可编辑；其余列由 setReadOnly 统一控制
                        item.setFlags(item.flags() & ~Qt.ItemIsEditable)
        finally:
            self._updating = False
            self.preview.blockSignals(signals_blocked)
            self.preview.setUpdatesEnabled(updates_enabled)

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(4)
        tb = QHBoxLayout()
        title = QLabel("字幕预览 · 前 100 行")
        title.setToolTip("处理时只显示连续的前 100 行，填满后停止自动刷新；编辑按钮可查看完整字幕。")
        title.setObjectName("panelTitle")
        tb.addWidget(title)
        self._edit_btn = action_button("✏ 编辑")
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
            "<div style='text-align:center;'>"
            f"<img src='{icon_data_url('video', '#64748b')}' width='32' height='32'>"
            "<br><br><b>暂无字幕</b>"
            "<br><br>添加或拖入 .srt 文件后，字幕会显示在这里"
            "</div>"
        )
        self._empty_label.setAlignment(Qt.AlignCenter)
        self._empty_label.setStyleSheet("color:#64748b; background:transparent; font-size:13px;")
        self._stack = QStackedWidget()
        self._stack.setStyleSheet("QStackedWidget { background: transparent; }")
        self._stack.addWidget(self._empty_label)
        self._stack.addWidget(self.preview)
        layout.addWidget(self._stack, 1)

        # 实时追加渲染节流：转写速度快时 preview_append 事件密集，
        # 每次都全量重建表格会卡 UI；200ms 内的多次追加合并为一次渲染
        self._render_timer = QTimer(self)
        self._render_timer.setSingleShot(True)
        self._render_timer.setInterval(200)
        self._render_timer.timeout.connect(self._flush_live_render)
        self._scroll_on_render = False

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
                    # 与 parse_srt / 列表加载同策略的自动编码识别（GBK/ANSI 中文字幕常见）
                    text = _read_text_auto(Path(path))
                    self.set_text(text)
                    self._last_output_dir = Path(path).parent
                    self._source_path = Path(path)
                    self.fileDropped.emit(path)
                except Exception as e:
                    logger.error(f"读取字幕文件失败: {e}")
                    QMessageBox.warning(self, "错误", f"读取字幕文件失败:\n{e}")
                break

    def connect_toolbar(self, save_cb):
        self._save_cb = save_cb

    def set_text(self, text: str, preserve_live_preview: bool = False):
        self._render_timer.stop()
        self._scroll_on_render = False
        self._raw_text = text
        self._preview_blocks = split_srt_blocks(text)
        if preserve_live_preview and self._frozen_preview_blocks is not None:
            self._preview_stale = self._preview_blocks[:MAX_LIVE_PREVIEW_BLOCKS] != self._frozen_preview_blocks
            self.setReadOnly(True)
            return  # 后台终版/译文更新只保存全文，不重绘已经冻结的预览。
        self._frozen_preview_blocks = None
        self._preview_stale = False
        self.setReadOnly(True)
        self._render_structured_preview()
        if preserve_live_preview and len(self._preview_blocks) >= MAX_LIVE_PREVIEW_BLOCKS:
            self._frozen_preview_blocks = self._preview_blocks[:MAX_LIVE_PREVIEW_BLOCKS]

    def clear(self):
        self._render_timer.stop()
        self._scroll_on_render = False
        self._raw_text = ""
        self._preview_blocks.clear()
        self._frozen_preview_blocks = None
        self._preview_stale = False
        self._source_path = None
        self._highlighted_rows.clear()
        self._updating = True
        try:
            self.preview.setRowCount(0)
        finally:
            self._updating = False
        self.setReadOnly(True)
        self._stack.setCurrentWidget(self._empty_label)

    def append(self, text: str):
        # _raw_text 保留全文（内存开销可忽略，编辑/保存需要完整内容）；
        # 渲染使用增量维护的块列表，避免每次刷新都重新切分全文。
        self._raw_text = f"{self._raw_text}\n\n{text}".strip()
        self._preview_blocks.extend(split_srt_blocks(text))
        if self._frozen_preview_blocks is not None:
            return
        self._scroll_on_render = True
        if not self._render_timer.isActive():
            self._render_timer.start()

    def _flush_live_render(self):
        """填满前 100 行后冻结，后续追加不启动定时器或渲染表格。"""
        self._render_timer.stop()
        if self._frozen_preview_blocks is not None:
            return
        self._render_structured_preview()
        if len(self._preview_blocks) >= MAX_LIVE_PREVIEW_BLOCKS:
            self._frozen_preview_blocks = self._preview_blocks[:MAX_LIVE_PREVIEW_BLOCKS]
        if self._scroll_on_render:
            self._scroll_on_render = False
            self.preview.scrollToBottom()

    def get_text(self) -> str:
        return self._raw_text

    def setReadOnly(self, readonly: bool):
        """控制表格是否允许直接编辑（完成后放开，供微调译文/时间轴）"""
        if readonly or self._preview_stale:
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
        signals_blocked = self.preview.blockSignals(True)
        try:
            for r in rows:
                for col in range(self.preview.columnCount()):
                    item = self.preview.item(r, col)
                    if item:
                        item.setBackground(QBrush(QColor(c["highlight_bg"])))
                        item.setForeground(QBrush(QColor(c["highlight_fg"])))
        finally:
            self.preview.blockSignals(signals_blocked)
        self._highlighted_rows = set(rows)
        if rows:
            self.preview.scrollToItem(self.preview.item(rows[0], 0), QAbstractItemView.PositionAtTop)

    def clear_highlight(self):
        """恢复高亮行的默认样式（交替背景 + 各列颜色）"""
        kinds = ["index", "time", "text", "translation"]
        colors = self._palette_colors()
        signals_blocked = self.preview.blockSignals(True)
        try:
            for r in self._highlighted_rows:
                for col in range(self.preview.columnCount()):
                    item = self.preview.item(r, col)
                    if item:
                        item.setBackground(QBrush())
                        self._style_item(item, kinds[col], colors)
        finally:
            self.preview.blockSignals(signals_blocked)
        self._highlighted_rows.clear()

    # ── 单元格编辑回写 _raw_text ──

    def _on_item_changed(self, item: QTableWidgetItem):
        """用户直接在表格里改时间轴/原文/译文后，同步回 _raw_text（保存走 get_text）"""
        if getattr(self, "_updating", False) or self._preview_stale:
            return
        if not self._raw_text.strip():
            return
        col = item.column()
        if col not in (1, 2, 3):
            return
        blocks = self._preview_blocks.copy()
        # 按渲染时记录的源块索引回写（渲染跳过了无效块，行号 ≠ 块序号）
        block_idx = item.data(Qt.UserRole)
        if not isinstance(block_idx, int) or block_idx >= len(blocks):
            return
        lines = blocks[block_idx].splitlines()
        timing_index = 0 if lines and SRT_TIMING_LINE_RE.match(lines[0]) else 1
        content_start = timing_index + 1
        if col == 1:
            # 时间轴
            if len(lines) <= timing_index:
                return
            lines[timing_index] = item.text().strip()
        else:
            text_lines = lines[content_start:]
            if not text_lines:
                return
            if col == 2:
                # 原文（保持第 1 行语义；译文原样保留）
                text_lines[0] = item.text()
                lines = lines[:content_start] + text_lines
            else:
                # 译文（可多行；清空或 “—” 视为无译文）
                new_trans = item.text()
                if new_trans.strip() in ("", "—"):
                    lines = lines[:content_start] + [text_lines[0]]
                else:
                    lines = lines[:content_start] + [text_lines[0]] + new_trans.split("\n")
        blocks[block_idx] = "\n".join(lines)
        body = "\n\n".join(blocks)
        # 保留原始文本首尾空白，避免重建丢失 SRT 结尾换行等格式
        leading = self._raw_text[: len(self._raw_text) - len(self._raw_text.lstrip())]
        trailing = self._raw_text[len(self._raw_text.rstrip()):]
        self._raw_text = leading + body + trailing
        self._preview_blocks = blocks
        if self._frozen_preview_blocks is not None and block_idx < len(self._frozen_preview_blocks):
            self._frozen_preview_blocks[block_idx] = blocks[block_idx]

    def _open_edit_dialog(self):
        content = self.get_text().strip()
        if not content:
            QMessageBox.information(self, "提示", "暂无字幕可编辑")
            return
        dlg = EditDialog(content, self, save_cb=self._save_cb)
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

    @property
    def source_path(self) -> Optional[Path]:
        """当前预览内容来源的字幕文件（保存时回写它）；实时预览等无来源时为 None"""
        return self._source_path

    @source_path.setter
    def source_path(self, path):
        self._source_path = path


class EditDialog(QDialog):
    """分页字幕编辑弹窗（按字幕段分页）"""

    def __init__(self, full_text: str, parent=None, save_cb=None):
        super().__init__(parent)
        self._full_text = full_text
        self._blocks = split_srt_blocks(full_text)
        self._page_size = 10
        self._current_page = 0
        self._total_pages = 0
        self._page_edits: Dict[int, str] = {}
        self._save_cb = save_cb
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

        self._prev_btn = action_button("◀", accessible_name="上一页")
        self._prev_btn.setFixedWidth(32)
        self._prev_btn.clicked.connect(self._prev_page)
        nav.addWidget(self._prev_btn)

        self._page_label = QLabel("0/0")
        nav.addWidget(self._page_label)

        self._next_btn = action_button("▶", icon="chevron-right", accessible_name="下一页")
        self._next_btn.setFixedWidth(32)
        self._next_btn.clicked.connect(self._next_page)
        nav.addWidget(self._next_btn)

        nav.addStretch()

        self._dirty_label = QLabel("")
        self._dirty_label.setObjectName("dialogWarning")
        nav.addWidget(self._dirty_label)

        layout.addLayout(nav)

        action_row = QHBoxLayout()
        self._find_btn = action_button("🔍 查找")
        self._find_btn.clicked.connect(self._find_in_editor)
        action_row.addWidget(self._find_btn)
        self._save_all_btn = action_button("💾 保存")
        self._save_all_btn.setObjectName("startBtn")
        self._save_all_btn.clicked.connect(self._save_all_and_exit)
        action_row.addWidget(self._save_all_btn)
        self._offset_btn = action_button("⏱ 偏移")
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
        self._close_btn = action_button("取消")
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
        self._save_edit_buffer()
        merged = self.get_merged_text()
        self._full_text = merged
        self._blocks = split_srt_blocks(merged)
        self._page_edits.clear()
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
        from .srt_utils import shift_srt_timestamps
        merged = shift_srt_timestamps(self.get_merged_text(), offset)
        self._full_text = merged
        self._blocks = split_srt_blocks(merged)
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
        title.setObjectName("panelTitle")
        title.setFixedHeight(20)
        layout.addWidget(title)
        self.log_list = QListWidget()
        self.log_list.setObjectName("logList")
        self.log_list.setMinimumHeight(60)
        self.log_list.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOn)
        self.log_list.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        # 日志不可选中：选中态没有功能消费方（复制走条目按钮、导出为全量），
        # 点某条后残留的高亮点击别处也不会清除；NoFocus 同时避免点日志抢走输入焦点
        self.log_list.setSelectionMode(QAbstractItemView.NoSelection)
        self.log_list.setFocusPolicy(Qt.NoFocus)
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
        QTimer.singleShot(0, lambda: item.setSizeHint(self._hint_for(entry)))

    def _hint_for(self, entry):
        """按当前可视宽度计算日志条目行高。

        QLabel 开启 wordWrap 后 sizeHint 高度与实际宽度无关：列表较窄时
        折行数比预估多，直接用 sizeHint 会把折行部分裁掉。布局完成后用
        heightForWidth(可视宽度) 重算；未布局（宽度无意义）时退回 sizeHint。
        """
        hint = entry.sizeHint()
        try:
            vw = self.log_list.viewport().width()
            if vw > 50:
                h = entry.heightForWidth(vw)
                if h and h > 0:
                    hint.setHeight(max(hint.height(), h))
        except Exception:
            pass
        return hint

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
                    it.setSizeHint(self._hint_for(w))
        finally:
            self._relayouting = False


class SignalBridge(QObject):
    event_received = Signal(object)

    def post(self, event: dict):
        self.event_received.emit(event)
