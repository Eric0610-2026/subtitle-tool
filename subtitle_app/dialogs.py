#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
对话框模块：设置、历史管理、缓存管理
"""
from pathlib import Path

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QGridLayout, QGroupBox,
    QLineEdit, QComboBox, QCheckBox, QPushButton, QListWidget,
    QListWidgetItem, QLabel, QSpinBox, QFileDialog, QMessageBox,
    QAbstractItemView, QTabWidget, QWidget, QFrame,
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont

from .srt_utils import load_json, save_json, IGNORE_FILE
from .config import cfg

_SCROLLBAR_STYLE = """
    QScrollBar:vertical { width:8px; background:transparent; border:none; }
    QScrollBar::handle:vertical { background:#c0c4cc; border-radius:4px; min-height:24px; }
    QScrollBar::handle:vertical:hover { background:#909399; }
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height:0; border:none; }
    QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background:none; }
    QScrollBar:horizontal { height:8px; background:transparent; border:none; }
    QScrollBar::handle:horizontal { background:#c0c4cc; border-radius:4px; min-width:24px; }
    QScrollBar::handle:horizontal:hover { background:#909399; }
    QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width:0; border:none; }
    QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal { background:none; }
"""


class SettingsDialog(QDialog):
    """二级设置对话框——语音识别 + AI翻译全部参数"""

    def __init__(self, parent, values: dict):
        super().__init__(parent)
        self.setStyleSheet(_SCROLLBAR_STYLE)
        self.setWindowTitle("更多设置")
        self.setMinimumWidth(520)
        layout = QVBoxLayout(self)
        layout.setSpacing(16)

        # ── 语音识别 ──
        sg1 = QGroupBox("🎙 语音识别")
        g1 = QGridLayout(sg1)
        g1.setVerticalSpacing(8)
        r = 0
        g1.addWidget(QLabel("模型目录"), r, 0)
        self.model_dir = QLineEdit(values.get("model_dir", ""))
        g1.addWidget(self.model_dir, r, 1)
        browse_btn = QPushButton("浏览...")
        browse_btn.clicked.connect(lambda: self.model_dir.setText(
            QFileDialog.getExistingDirectory(self, "选择模型目录", self.model_dir.text())))
        g1.addWidget(browse_btn, r, 2)
        r += 1
        g1.addWidget(QLabel("识别语言"), r, 0)
        self.lang = QComboBox()
        self.lang.addItems(["auto", "zh", "en", "ja", "ko", "fr", "de", "es", "ru"])
        self.lang.setCurrentText(values.get("language", "auto"))
        g1.addWidget(self.lang, r, 1)
        g1.addWidget(QLabel("auto=自动检测"), r, 2)
        r += 1
        g1.addWidget(QLabel("设备"), r, 0)
        self.device = QComboBox()
        self.device.addItems(["cuda", "cpu"])
        self.device.setCurrentText(values.get("device", "cuda"))
        g1.addWidget(self.device, r, 1)
        r += 1
        g1.addWidget(QLabel("精度"), r, 0)
        self.precision = QComboBox()
        self.precision.addItems(["int8_float16", "float16", "int8", "float32"])
        self.precision.setCurrentText(values.get("compute_type", "int8_float16"))
        g1.addWidget(self.precision, r, 1)
        r += 1
        opts_row = QHBoxLayout()
        self.extract_cb = QCheckBox("提取音频")
        self.extract_cb.setChecked(values.get("extract_audio", True))
        opts_row.addWidget(self.extract_cb)
        self.vad_cb = QCheckBox("VAD 过滤")
        self.vad_cb.setChecked(values.get("vad_filter", True))
        opts_row.addWidget(self.vad_cb)
        self.pipeline_cb = QCheckBox("并行流水线")
        self.pipeline_cb.setChecked(values.get("pipeline", True))
        self.pipeline_cb.setToolTip("勾选：转写 N+1 与翻译 N 同时进行（节省时间）\n不勾：完全串行处理")
        opts_row.addWidget(self.pipeline_cb)
        opts_row.addStretch()
        g1.addLayout(opts_row, r, 0, 1, 3)
        layout.addWidget(sg1)

        # ── AI 翻译 ──
        sg2 = QGroupBox("🌍 AI 翻译")
        g2 = QGridLayout(sg2)
        g2.setVerticalSpacing(8)
        r = 0
        g2.addWidget(QLabel("目标语言"), r, 0)
        self.target_lang = QComboBox()
        self.target_lang.addItems(["zh", "en", "ja", "ko", "fr", "de", "es", "ru"])
        self.target_lang.setCurrentText(values.get("target_lang", "zh"))
        g2.addWidget(self.target_lang, r, 1, 1, 2)
        r += 1
        g2.addWidget(QLabel("模型"), r, 0)
        self.model_name = QLineEdit(values.get("translation_model", cfg.translation.model))
        g2.addWidget(self.model_name, r, 1, 1, 2)
        r += 1
        g2.addWidget(QLabel("API URL"), r, 0)
        self.api_url = QLineEdit(values.get("api_url", cfg.translation.api_url))
        g2.addWidget(self.api_url, r, 1, 1, 2)
        r += 1
        g2.addWidget(QLabel("API Key"), r, 0)
        self.api_key = QLineEdit(values.get("api_key", cfg.translation.api_key))
        self.api_key.setEchoMode(QLineEdit.Password)
        g2.addWidget(self.api_key, r, 1, 1, 2)
        r += 1
        self.only_zh_cb = QCheckBox("只要译文（不生成双语）")
        self.only_zh_cb.setChecked(values.get("translation_only", False))
        g2.addWidget(self.only_zh_cb, r, 0, 1, 3)
        r += 1
        g2.addWidget(QLabel("批大小"), r, 0)
        self.batch_size = QSpinBox()
        self.batch_size.setRange(10, 100)
        self.batch_size.setSingleStep(5)
        self.batch_size.setValue(values.get("translation_batch_size", cfg.translation.batch_size))
        g2.addWidget(self.batch_size, r, 1, 1, 2)
        r += 1
        self.pause_embed_cb = QCheckBox("嵌入前暂停确认（可预览/编辑字幕后再嵌入）")
        self.pause_embed_cb.setChecked(values.get("pause_before_embed", False))
        self.pause_embed_cb.setToolTip("翻译完成后弹出对话框，确认或编辑字幕内容后再嵌入 MKV")
        g2.addWidget(self.pause_embed_cb, r, 0, 1, 3)
        layout.addWidget(sg2)

        layout.addStretch()
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        cancel_btn = QPushButton("取消")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)
        session_btn = QPushButton("💾 本次有效")
        btn_row.addWidget(session_btn)
        permanent_btn = QPushButton("💾 永久保存")
        permanent_btn.setObjectName("startBtn")
        btn_row.addWidget(permanent_btn)
        layout.addLayout(btn_row)

        session_btn.clicked.connect(lambda: self.done(1))
        permanent_btn.clicked.connect(lambda: self.done(2))

    def get_values(self) -> dict:
        return {
            "model_dir": self.model_dir.text().strip(),
            "language": self.lang.currentText(),
            "device": self.device.currentText(),
            "compute_type": self.precision.currentText(),
            "extract_audio": self.extract_cb.isChecked(),
            "vad_filter": self.vad_cb.isChecked(),
            "target_lang": self.target_lang.currentText(),
            "translation_model": self.model_name.text().strip(),
            "api_url": self.api_url.text().strip(),
            "api_key": self.api_key.text().strip(),
            "pipeline": self.pipeline_cb.isChecked(),
            "translation_only": self.only_zh_cb.isChecked(),
            "translation_batch_size": self.batch_size.value(),
            "pause_before_embed": self.pause_embed_cb.isChecked(),
        }


def show_history_dialog(parent, work_dir: str, log_callback) -> None:
    path = Path(work_dir) / IGNORE_FILE
    data = load_json(path, {})
    done = data.get("done", [])
    ignored = data.get("ignored", [])
    file_cost = data.get("file_cost", {})
    if not done and not ignored:
        box = QMessageBox(parent)
        box.setWindowTitle("处理历史")
        box.setText("尚无记录")
        box.setIcon(QMessageBox.NoIcon)
        box.exec()
        return
    dlg = QDialog(parent)
    dlg.setStyleSheet(_SCROLLBAR_STYLE)
    dlg.setWindowTitle(f"处理历史 ({len(done)} 已完成, {len(ignored)} 已忽略)")
    dlg.resize(640, 480)
    layout = QVBoxLayout(dlg)
    tabs = QTabWidget()
    layout.addWidget(tabs)
    btn_row = QHBoxLayout()
    layout.addLayout(btn_row)
    btn_row.addStretch()
    close_btn = QPushButton("关闭")
    close_btn.clicked.connect(dlg.accept)
    btn_row.addWidget(close_btn)

    # ── 已完成标签页 ──
    done_widget = QWidget()
    done_layout = QVBoxLayout(done_widget)
    hint = QLabel("选中条目后点击「删除选中」可移除记录（不影响已生成的字幕文件）")
    hint.setStyleSheet("color:#64748b; font-size:11px;")
    done_layout.addWidget(hint)
    done_list = QListWidget()
    done_list.setSelectionMode(QAbstractItemView.ExtendedSelection)
    done_list.setFont(QFont("Consolas", 9))
    for entry in done:
        if isinstance(entry, dict):
            p = entry.get("path", "")
        else:
            p = entry
        ci = file_cost.get(p, {})
        total = ci.get("total_cost", 0)
        tokens_in = ci.get("prompt_tokens", 0)
        if total:
            display = f"{p}   ¥{total:.4f}  ({tokens_in:,} tokens)"
        else:
            display = p
        item = QListWidgetItem(display)
        item.setData(Qt.UserRole, p)
        done_list.addItem(item)
    done_layout.addWidget(done_list, 1)
    btn_row1 = QHBoxLayout()
    del_done_btn = QPushButton("🗑 删除选中")
    del_done_btn.setObjectName("stopBtn")
    btn_row1.addWidget(del_done_btn)
    btn_row1.addStretch()
    done_layout.addLayout(btn_row1)
    tabs.addTab(done_widget, f"已完成 ({len(done)})")

    # ── 已忽略标签页 ──
    ignore_widget = QWidget()
    ignore_layout = QVBoxLayout(ignore_widget)
    hint2 = QLabel("选中条目后点击「取消忽略」恢复文件")
    hint2.setStyleSheet("color:#64748b; font-size:11px;")
    ignore_layout.addWidget(hint2)
    ignore_list = QListWidget()
    ignore_list.setSelectionMode(QAbstractItemView.ExtendedSelection)
    ignore_list.setFont(QFont("Consolas", 9))
    for p in ignored:
        item = QListWidgetItem(p)
        item.setData(Qt.UserRole, p)
        ignore_list.addItem(item)
    ignore_layout.addWidget(ignore_list, 1)
    btn_row2 = QHBoxLayout()
    unignore_btn = QPushButton("↩ 取消忽略选中")
    unignore_btn.setObjectName("accentBtn")
    btn_row2.addWidget(unignore_btn)
    btn_row2.addStretch()
    ignore_layout.addLayout(btn_row2)
    tabs.addTab(ignore_widget, f"已忽略 ({len(ignored)})")

    def _delete_done():
        sel = done_list.selectedItems()
        if not sel:
            return
        box = QMessageBox(dlg)
        box.setWindowTitle("删除确认")
        box.setText(f"确定从历史中移除选中的 {len(sel)} 条？")
        box.setIcon(QMessageBox.NoIcon)
        box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
        if box.exec() != QMessageBox.Yes:
            return
        for item in reversed(sorted(sel, key=lambda x: done_list.row(x))):
            done_list.takeItem(done_list.row(item))
        remaining = []
        for i in range(done_list.count()):
            item = done_list.item(i)
            p = item.data(Qt.UserRole) or item.text()
            remaining.append(p)
        data["done"] = remaining
        costs = data.get("file_cost", {})
        remaining_set = set(remaining)
        for k in list(costs.keys()):
            if k not in remaining_set:
                del costs[k]
        save_json(path, data)
        tabs.setTabText(0, f"已完成 ({len(remaining)})")
        dlg.setWindowTitle(f"处理历史 ({len(remaining)} 已完成, {len(ignored)} 已忽略)")
        log_callback(f"已从历史中移除 {len(sel)} 条记录")

    def _unignore_selected():
        sel = ignore_list.selectedItems()
        if not sel:
            return
        removed = 0
        for item in reversed(sorted(sel, key=lambda x: ignore_list.row(x))):
            p = item.data(Qt.UserRole) or item.text()
            row = ignore_list.row(item)
            ignore_list.takeItem(row)
            if p in ignored:
                ignored.remove(p)
            removed += 1
        data["ignored"] = ignored
        save_json(path, data)
        tabs.setTabText(1, f"已忽略 ({len(ignored)})")
        dlg.setWindowTitle(f"处理历史 ({len(data.get('done', []))} 已完成, {len(ignored)} 已忽略)")
        log_callback(f"已取消忽略 {removed} 个文件")

    del_done_btn.clicked.connect(_delete_done)
    unignore_btn.clicked.connect(_unignore_selected)
    dlg.exec()


def show_cache_dialog(parent, work_dir: str, log_callback) -> None:
    """显示翻译缓存弹窗，支持逐条删除和全部清空"""
    path = Path(work_dir) / ".subtitle_translation_cache.json"
    cache = load_json(path, {})
    size = path.stat().st_size if path.exists() else 0
    dlg = QDialog(parent)
    dlg.setStyleSheet(_SCROLLBAR_STYLE)
    dlg.setWindowTitle("翻译缓存管理")
    dlg.resize(480, 400)
    layout = QVBoxLayout(dlg)
    info = QLabel(f"缓存条目：{len(cache)} 条　　缓存大小：{size/1024:.1f} KB")
    layout.addWidget(info)
    hint = QLabel("选中条目后点击「删除选中」可逐条移除；「清空缓存」则全部清除")
    hint.setStyleSheet("color:#64748b; font-size:11px;")
    layout.addWidget(hint)
    list_widget = QListWidget()
    list_widget.setSelectionMode(QAbstractItemView.ExtendedSelection)
    list_widget.setFont(QFont("Consolas", 9))
    cache_keys = []
    for i, (k, v) in enumerate(sorted(cache.items()), 1):
        list_widget.addItem(f"{i:>4}. {v[:80]}")
        cache_keys.append(k)
    layout.addWidget(list_widget, 1)
    btn_row = QHBoxLayout()
    del_btn = QPushButton("🗑 删除选中")
    del_btn.setObjectName("stopBtn")
    btn_row.addWidget(del_btn)
    clear_btn = QPushButton("🗑 清空缓存")
    clear_btn.setObjectName("stopBtn")
    btn_row.addWidget(clear_btn)
    btn_row.addStretch()
    close_btn = QPushButton("关闭")
    close_btn.clicked.connect(dlg.accept)
    btn_row.addWidget(close_btn)
    layout.addLayout(btn_row)

    def _delete_selected():
        nonlocal cache_keys
        sel = list_widget.selectedItems()
        if not sel:
            return
        box = QMessageBox(dlg)
        box.setWindowTitle("删除确认")
        box.setText(f"确定从缓存中移除选中的 {len(sel)} 条？")
        box.setIcon(QMessageBox.NoIcon)
        box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
        if box.exec() != QMessageBox.Yes:
            return
        indices = set()
        for item in sel:
            indices.add(list_widget.row(item))
        new_cache = {}
        for i, k in enumerate(cache_keys):
            if i not in indices:
                new_cache[k] = cache[k]
        save_json(path, new_cache)
        cache.clear()
        cache.update(new_cache)
        for item in reversed(sorted(sel, key=lambda x: list_widget.row(x))):
            list_widget.takeItem(list_widget.row(item))
        cache_keys = [k for k in sorted(new_cache.keys())]
        for j in range(list_widget.count()):
            text = list_widget.item(j).text()
            dot_pos = text.find(". ")
            display = text[dot_pos + 2:] if dot_pos > 0 else text
            list_widget.item(j).setText(f"{j+1:>4}. {display}")
        dlg.setWindowTitle(f"翻译缓存管理 ({list_widget.count()} 条)")
        size_after = path.stat().st_size if path.exists() else 0
        info.setText(f"缓存条目：{list_widget.count()} 条　　缓存大小：{size_after/1024:.1f} KB")
        log_callback(f"已从缓存中移除 {len(sel)} 条")

    del_btn.clicked.connect(_delete_selected)
    clear_btn.clicked.connect(lambda: _clear_all(dlg, path, info, list_widget, log_callback))
    dlg.exec()


def _clear_all(dlg, path, info_label, list_widget, log_callback):
    save_json(path, {})
    log_callback("翻译缓存已清空")
    if list_widget:
        list_widget.clear()
    if info_label:
        info_label.setText("缓存条目：0 条　　缓存大小：0.0 KB")
    dlg.accept()


# ─── 嵌入字幕对话框 ────────────────────────────────────────


def _find_matching_subtitle(video_path: Path) -> Path:
    """查找与视频同名的字幕文件，优先精确匹配，其次忽略语言标签"""
    parent = video_path.parent
    stem = video_path.stem
    # 1. 精确匹配 {stem}.srt
    exact = parent / f"{stem}.srt"
    if exact.exists():
        return exact
    # 2. 匹配带语言标签的 {stem}.xx.srt / {stem}.xx-xx.srt
    for f in sorted(parent.glob(f"{stem}.*.srt")):
        return f
    return None


def _find_matching_video(subtitle_path: Path) -> Path:
    """查找与字幕同名的视频文件，忽略字幕的语言标签"""
    parent = subtitle_path.parent
    stem = subtitle_path.stem
    exts = cfg.srt.video_exts
    # 1. 先用完整 stem 匹配
    for ext in exts:
        candidate = parent / f"{stem}{ext}"
        if candidate.exists():
            return candidate
    # 2. stem 含点号（语言标签），去掉最后一段再试
    if "." in stem:
        base = stem.rsplit(".", 1)[0]
        for ext in exts:
            candidate = parent / f"{base}{ext}"
            if candidate.exists():
                return candidate
    return None


class EmbedDialog(QDialog):
    """嵌入字幕对话框：上下两行分别选视频和字幕，自动匹配同名文件，支持批量"""

    def __init__(self, parent, default_dir: str = ""):
        super().__init__(parent)
        self.setWindowTitle("📦 嵌入字幕")
        self.setMinimumSize(620, 400)
        self.resize(680, 460)
        self._default_dir = default_dir
        self._pairs = []  # [(video_path, subtitle_path), ...]
        self._setup_ui()
        self._apply_style()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        # ── 标题 ──
        title = QLabel("📦 嵌入字幕 — 将字幕嵌入视频文件为 MKV")
        title.setStyleSheet("font-size:14px; font-weight:600;")
        layout.addWidget(title)

        # ── 嵌入列表 ──
        list_label = QLabel("嵌入任务列表：")
        list_label.setStyleSheet("font-weight:600;")
        layout.addWidget(list_label)

        self.table = QListWidget()
        self.table.setAlternatingRowColors(True)
        self.table.setMinimumHeight(120)
        layout.addWidget(self.table, 1)

        # ── 分隔线 ──
        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setFrameShadow(QFrame.Sunken)
        layout.addWidget(sep)

        # ── 添加新任务 ──
        add_label = QLabel("添加新任务：")
        add_label.setStyleSheet("font-weight:600;")
        layout.addWidget(add_label)

        # 视频行
        video_row = QHBoxLayout()
        video_row.addWidget(QLabel("视频:"))
        self.video_path = QLineEdit()
        self.video_path.setPlaceholderText("选择视频文件...")
        video_row.addWidget(self.video_path, 1)
        video_btn = QPushButton("📂 浏览")
        video_btn.clicked.connect(self._browse_video)
        video_row.addWidget(video_btn)
        layout.addLayout(video_row)

        # 字幕行
        srt_row = QHBoxLayout()
        srt_row.addWidget(QLabel("字幕:"))
        self.srt_path = QLineEdit()
        self.srt_path.setPlaceholderText("选择字幕文件...")
        srt_row.addWidget(self.srt_path, 1)
        srt_btn = QPushButton("📂 浏览")
        srt_btn.clicked.connect(self._browse_srt)
        srt_row.addWidget(srt_btn)
        layout.addLayout(srt_row)

        # 操作按钮行
        btn_row = QHBoxLayout()
        add_pair_btn = QPushButton("➕ 添加任务")
        add_pair_btn.clicked.connect(self._add_pair)
        add_pair_btn.setObjectName("accentBtn")
        btn_row.addWidget(add_pair_btn)
        btn_row.addStretch()
        self.clear_btn = QPushButton("🗑 清空列表")
        self.clear_btn.clicked.connect(self._clear_list)
        self.clear_btn.setObjectName("stopBtn")
        btn_row.addWidget(self.clear_btn)
        layout.addLayout(btn_row)

        # ── 分隔线 ──
        sep2 = QFrame()
        sep2.setFrameShape(QFrame.HLine)
        sep2.setFrameShadow(QFrame.Sunken)
        layout.addWidget(sep2)

        # ── 底部按钮 ──
        bottom_row = QHBoxLayout()
        self.count_label = QLabel("共 0 个任务")
        self.count_label.setStyleSheet("color:#64748b;")
        bottom_row.addWidget(self.count_label)
        bottom_row.addStretch()
        self.start_btn = QPushButton("▶ 开始嵌入")
        self.start_btn.setObjectName("startBtn")
        self.start_btn.setEnabled(False)
        self.start_btn.clicked.connect(self._start_embed)
        self.start_btn.setFixedHeight(36)
        bottom_row.addWidget(self.start_btn)
        close_btn = QPushButton("✕ 关闭")
        close_btn.clicked.connect(self.reject)
        close_btn.setFixedHeight(36)
        bottom_row.addWidget(close_btn)
        layout.addLayout(bottom_row)

    def _browse_video(self):
        """浏览视频文件，选中后自动查找同名字幕"""
        exts = " ".join(f"*{e}" for e in cfg.srt.video_exts)
        start = self.video_path.text() or self._default_dir
        path, _ = QFileDialog.getOpenFileName(
            self, "选择视频文件", start, f"视频文件 ({exts})")
        if not path:
            return
        self.video_path.setText(path)
        # 自动查找同名字幕
        vp = Path(path)
        matched = _find_matching_subtitle(vp)
        if matched:
            self.srt_path.setText(str(matched))
        else:
            # 可选：清空字幕行，让用户手动选择
            self.srt_path.clear()

    def _browse_srt(self):
        """浏览字幕文件，选中后自动查找同名视频"""
        start = self.srt_path.text() or self._default_dir
        path, _ = QFileDialog.getOpenFileName(
            self, "选择字幕文件", start, "字幕文件 (*.srt)")
        if not path:
            return
        self.srt_path.setText(path)
        # 自动查找同名视频
        sp = Path(path)
        matched = _find_matching_video(sp)
        if matched and not self.video_path.text():
            self.video_path.setText(str(matched))

    def _add_pair(self):
        """将当前视频+字幕添加到列表"""
        v = self.video_path.text().strip()
        s = self.srt_path.text().strip()
        if not v or not s:
            QMessageBox.warning(self, "提示", "请先选择视频和字幕文件")
            return
        vp = Path(v)
        sp = Path(s)
        if not vp.exists():
            QMessageBox.warning(self, "提示", f"视频文件不存在：{v}")
            return
        if not sp.exists():
            QMessageBox.warning(self, "提示", f"字幕文件不存在：{s}")
            return
        if sp.suffix.lower() != ".srt":
            QMessageBox.warning(self, "提示", "字幕文件必须是 .srt 格式")
            return
        # 检查是否已添加
        for existing_v, existing_s in self._pairs:
            if existing_v == vp and existing_s == sp:
                QMessageBox.warning(self, "提示", "该任务已存在")
                return
        self._pairs.append((vp, sp))
        self._refresh_table()
        self.video_path.clear()
        self.srt_path.clear()

    def _clear_list(self):
        if not self._pairs:
            return
        self._pairs.clear()
        self._refresh_table()

    def _refresh_table(self):
        self.table.clear()
        for i, (v, s) in enumerate(self._pairs, 1):
            item = QListWidgetItem(f"{i}.  {v.name}  →  {s.name}")
            item.setData(Qt.UserRole, i - 1)
            self.table.addItem(item)
        count = len(self._pairs)
        self.count_label.setText(f"共 {count} 个任务")
        self.start_btn.setEnabled(count > 0)

    def _start_embed(self):
        """开始批量嵌入"""
        if not self._pairs:
            return
        self.accept()

    def get_pairs(self):
        """返回所有 (视频路径, 字幕路径) 对"""
        return self._pairs.copy()

    def _apply_style(self):
        self.setStyleSheet("""
            QListWidget { font-size:12px; }
            QListWidget::item { padding:4px 8px; }
            QPushButton#startBtn {
                background:#22c55e; color:white; border:none;
                border-radius:6px; padding:8px 20px; font-weight:bold; font-size:13px;
            }
            QPushButton#startBtn:hover { background:#16a34a; }
            QPushButton#startBtn:disabled { background:#94a3b8; }
            QPushButton#accentBtn {
                background:#6366f1; color:white; border:none;
                border-radius:4px; padding:6px 14px;
            }
            QPushButton#accentBtn:hover { background:#4f46e5; }
            QPushButton#stopBtn {
                background:#ef4444; color:white; border:none;
                border-radius:4px; padding:6px 14px;
            }
            QPushButton#stopBtn:hover { background:#dc2626; }
            QLineEdit { padding:4px 6px; border:1px solid #e2e8f0; border-radius:4px; }
        """)
