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
    QAbstractItemView,
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont

from .srt_utils import load_json, save_json
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
        }


def show_history_dialog(parent, work_dir: str, log_callback) -> None:
    path = Path(work_dir) / ".subtitle_progress.json"
    data = load_json(path, {})
    done = data.get("done", [])
    file_cost = data.get("file_cost", {})
    if not done:
        box = QMessageBox(parent)
        box.setWindowTitle("处理历史")
        box.setText("尚无处理记录")
        box.setIcon(QMessageBox.NoIcon)
        box.exec()
        return
    dlg = QDialog(parent)
    dlg.setStyleSheet(_SCROLLBAR_STYLE)
    dlg.setWindowTitle(f"处理历史 ({len(done)} 条)")
    dlg.resize(640, 400)
    layout = QVBoxLayout(dlg)
    hint = QLabel("选中条目后点击「删除选中」可移除记录（不影响已生成的字幕文件）")
    hint.setStyleSheet("color:#64748b; font-size:11px;")
    layout.addWidget(hint)
    list_widget = QListWidget()
    list_widget.setSelectionMode(QAbstractItemView.ExtendedSelection)
    list_widget.setFont(QFont("Consolas", 9))
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
        list_widget.addItem(item)
    layout.addWidget(list_widget, 1)
    btn_row = QHBoxLayout()
    del_btn = QPushButton("🗑 删除选中")
    del_btn.setObjectName("stopBtn")
    btn_row.addWidget(del_btn)
    btn_row.addStretch()
    close_btn = QPushButton("关闭")
    close_btn.clicked.connect(dlg.accept)
    btn_row.addWidget(close_btn)
    layout.addLayout(btn_row)

    def _delete_selected():
        sel = list_widget.selectedItems()
        if not sel:
            return
        box = QMessageBox(dlg)
        box.setWindowTitle("删除确认")
        box.setText(f"确定从历史记录中移除选中的 {len(sel)} 条？\n（不影响已生成的字幕文件）")
        box.setIcon(QMessageBox.NoIcon)
        box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
        if box.exec() != QMessageBox.Yes:
            return
        for item in reversed(sorted(sel, key=lambda x: list_widget.row(x))):
            list_widget.takeItem(list_widget.row(item))
        remaining = []
        for i in range(list_widget.count()):
            item = list_widget.item(i)
            p = item.data(Qt.UserRole) or item.text()
            remaining.append(p)
        data["done"] = remaining
        costs = data.get("file_cost", {})
        remaining_set = set(remaining)
        for k in list(costs.keys()):
            if k not in remaining_set:
                del costs[k]
        save_json(path, data)
        dlg.setWindowTitle(f"处理历史 ({len(remaining)} 条)")
        log_callback(f"已从历史中移除 {len(sel)} 条记录")

    del_btn.clicked.connect(_delete_selected)
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
