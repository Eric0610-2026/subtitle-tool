#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
对话框模块：设置、历史管理、缓存管理
"""
import glob
from pathlib import Path

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QGridLayout, QGroupBox,
    QLineEdit, QComboBox, QCheckBox, QPushButton, QListWidget,
    QListWidgetItem, QLabel, QSpinBox, QFileDialog, QMessageBox,
    QAbstractItemView, QTabWidget, QWidget, QFrame, QTextEdit,
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont

from .srt_utils import load_json, save_json, IGNORE_FILE
from .config import cfg
from .local_service import service_url_prefix
from .widgets import attach_popup_fade
from .translation import (
    LANG_NAMES, clear_shared_cache, remove_shared_cache_entries, shared_cache_snapshot,
)

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
    """二级设置对话框——语音识别 + AI翻译（本地 Hy-MT2）参数"""

    def __init__(self, parent, values: dict, history_cb=None, cache_cb=None):
        super().__init__(parent)
        self.setStyleSheet(_SCROLLBAR_STYLE + """
            QCheckBox { background: transparent; spacing: 7px; }
            QCheckBox::indicator { width: 16px; height: 16px; }
        """)
        self.setWindowTitle("更多设置")
        self.setMinimumWidth(520)
        self._history_cb = history_cb
        self._cache_cb = cache_cb
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
        # 展示中文名、userData 存代号（whisper 识别与配置存储仍用 auto/zh/en…）
        self.lang.addItem("自动检测", "auto")
        for code in ["zh", "en", "ja", "ko", "fr", "de", "es", "ru"]:
            self.lang.addItem(LANG_NAMES.get(code, code), code)
        self.lang.setCurrentIndex(max(0, self.lang.findData(values.get("language", "auto"))))
        g1.addWidget(self.lang, r, 1)
        r += 1
        opts_row = QHBoxLayout()
        opts_row.setSpacing(18)
        self.extract_cb = QCheckBox("提取音频")
        self.extract_cb.setChecked(values.get("extract_audio", True))
        opts_row.addWidget(self.extract_cb)
        self.vad_cb = QCheckBox("VAD 过滤")
        self.vad_cb.setChecked(values.get("vad_filter", True))
        opts_row.addWidget(self.vad_cb)
        opts_row.addStretch()
        g1.addLayout(opts_row, r, 0, 1, 3)
        r += 1

        # ── 默认视频目录（用于「📌 默认」按钮）──
        g1.addWidget(QLabel("默认视频目录"), r, 0)
        self.default_dir = QLineEdit(values.get("default_video_dir", ""))
        self.default_dir.setPlaceholderText("可留空；用于「📌 默认」按钮")
        g1.addWidget(self.default_dir, r, 1)
        dir_browse_btn = QPushButton("浏览...")
        dir_browse_btn.clicked.connect(lambda: self.default_dir.setText(
            QFileDialog.getExistingDirectory(self, "选择默认视频目录", self.default_dir.text())))
        g1.addWidget(dir_browse_btn, r, 2)
        r += 1

        # ── 语言检测复用开关 ──
        self.reuse_lang_cb = QCheckBox("复用同批语言检测结果（单一语言目录更快）")
        self.reuse_lang_cb.setChecked(values.get("reuse_auto_lang", True))
        self.reuse_lang_cb.setToolTip("勾选后：auto 模式下第一个文件的检测语言将复用到同批后续文件，"
                                      "跳过重复检测；混合语言目录请保持关闭")
        g1.addWidget(self.reuse_lang_cb, r, 0, 1, 3)
        layout.addWidget(sg1)

        # ── AI 翻译（本地 Hy-MT2）──
        sg2 = QGroupBox("🌍 AI 翻译（本地 Hy-MT2）")
        g2 = QGridLayout(sg2)
        g2.setVerticalSpacing(8)
        r = 0
        mode_hint = QLabel(f"使用本地 Hy-MT2 翻译服务 {service_url_prefix()}（自动启动，无需配置 API）")
        mode_hint.setStyleSheet("color:#22c55e; font-size:11px;")
        mode_hint.setWordWrap(True)
        g2.addWidget(mode_hint, r, 0, 1, 3)
        r += 1

        g2.addWidget(QLabel("目标语言"), r, 0)
        self.target_lang = QComboBox()
        for code in ["zh", "en", "ja", "ko", "fr", "de", "es", "ru"]:
            self.target_lang.addItem(LANG_NAMES.get(code, code), code)
        self.target_lang.setCurrentIndex(max(0, self.target_lang.findData(values.get("target_lang", "zh"))))
        g2.addWidget(self.target_lang, r, 1, 1, 2)
        r += 1

        self.only_zh_cb = QCheckBox("只要译文（不生成双语）")
        self.only_zh_cb.setChecked(values.get("translation_only", False))
        g2.addWidget(self.only_zh_cb, r, 0, 1, 3)
        r += 1

        g2.addWidget(QLabel("批大小"), r, 0)
        batch_row = QHBoxLayout()
        batch_row.setSpacing(4)
        self.batch_size = QSpinBox()
        self.batch_size.setRange(10, 5000)
        self.batch_size.setSingleStep(5)
        _default_bs = cfg.translation.batch_size or 20
        _saved_bs = values.get("translation_batch_size")
        self.batch_size.setValue(_default_bs if _saved_bs is None else _saved_bs)
        batch_row.addWidget(self.batch_size)
        self.send_all_cb = QCheckBox("一次性发送全部文本（不拆分批次）")
        self.send_all_cb.setChecked(values.get("send_all", False))
        self.send_all_cb.toggled.connect(self._on_send_all_toggled)
        batch_row.addWidget(self.send_all_cb)
        batch_row.addStretch()
        g2.addLayout(batch_row, r, 1, 1, 2)
        r += 1

        self.pause_embed_cb = QCheckBox("嵌入前暂停确认（可预览/编辑字幕后再嵌入）")
        self.pause_embed_cb.setChecked(values.get("pause_before_embed", False))
        self.pause_embed_cb.setToolTip("翻译完成后弹出对话框，确认或编辑字幕内容后再嵌入 MKV")
        g2.addWidget(self.pause_embed_cb, r, 0, 1, 3)
        r += 1

        # ── 字幕备份保留份数 ──
        g2.addWidget(QLabel("字幕备份份数"), r, 0)
        self.backup_max = QSpinBox()
        self.backup_max.setRange(0, 10000)
        self.backup_max.setValue(values.get("backup_max_files", 50))
        self.backup_max.setToolTip("logs/srt_backup 中保留的最近备份份数，超出自动清理最旧；0=不清理")
        g2.addWidget(self.backup_max, r, 1, 1, 2)
        layout.addWidget(sg2)

        # ── 数据管理（历史记录 / 翻译缓存）──
        if self._history_cb or self._cache_cb:
            sg3 = QGroupBox("🗂 数据管理")
            g3 = QHBoxLayout(sg3)
            g3.setContentsMargins(8, 6, 8, 6)
            g3.setSpacing(8)
            if self._history_cb:
                history_btn = QPushButton("📋 处理历史…")
                history_btn.setObjectName("bottomBtn")
                history_btn.setToolTip("查看已处理/已忽略文件记录；删除记录可让该文件重新被「重试」处理")
                history_btn.clicked.connect(lambda: self._history_cb())
                g3.addWidget(history_btn)
            if self._cache_cb:
                cache_btn = QPushButton("🗑 翻译缓存…")
                cache_btn.setObjectName("bottomBtn")
                cache_btn.setToolTip("查看/逐条删除/清空句子级翻译缓存（已翻译句子复用，一般无需清理）")
                cache_btn.clicked.connect(lambda: self._cache_cb())
                g3.addWidget(cache_btn)
            g3.addStretch()
            layout.addWidget(sg3)

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

        # 下拉弹层淡入过渡
        attach_popup_fade(self.lang)
        attach_popup_fade(self.target_lang)

    def _on_send_all_toggled(self, checked: bool):
        """一次性发送开关：勾选后禁用批大小调节"""
        self.batch_size.setEnabled(not checked)
        if checked:
            self.batch_size.setStyleSheet("color:#94a3b8;")
        else:
            self.batch_size.setStyleSheet("")

    def _batch_size_value(self):
        """批大小保存规则：等于 config 默认值时存 None（跟随配置），
        用户自定义值则保存覆盖"""
        default_bs = cfg.translation.batch_size or 20
        val = self.batch_size.value()
        return None if val == default_bs else val

    def get_values(self) -> dict:
        return {
            "model_dir": self.model_dir.text().strip(),
            "language": self.lang.currentData(),
            "extract_audio": self.extract_cb.isChecked(),
            "vad_filter": self.vad_cb.isChecked(),
            "default_video_dir": self.default_dir.text().strip(),
            "reuse_auto_lang": self.reuse_lang_cb.isChecked(),
            "target_lang": self.target_lang.currentData(),
            "translation_only": self.only_zh_cb.isChecked(),
            "translation_batch_size": self._batch_size_value(),
            "send_all": self.send_all_cb.isChecked(),
            "pause_before_embed": self.pause_embed_cb.isChecked(),
            "backup_max_files": self.backup_max.value(),
        }


def show_history_dialog(parent, work_dir: str, log_callback) -> None:
    path = Path(work_dir) / IGNORE_FILE
    data = load_json(path, {})
    done = data.get("done", [])
    ignored = data.get("ignored", [])
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
    dlg.resize(600, 400)
    layout = QVBoxLayout(dlg)
    layout.setContentsMargins(8, 8, 8, 8)
    layout.setSpacing(4)
    tabs = QTabWidget()
    layout.addWidget(tabs, 1)

    # ── 已完成标签页 ──
    done_widget = QWidget()
    done_layout = QVBoxLayout(done_widget)
    done_layout.setContentsMargins(4, 4, 4, 4)
    done_layout.setSpacing(4)
    done_list = QListWidget()
    done_list.setSelectionMode(QAbstractItemView.ExtendedSelection)
    done_list.setFont(QFont("Consolas", 9))
    for entry in done:
        if isinstance(entry, dict):
            p = entry.get("path", "")
        else:
            p = entry
        item = QListWidgetItem(Path(p).name)
        item.setData(Qt.UserRole, p)
        done_list.addItem(item)
    done_layout.addWidget(done_list, 1)
    del_done_btn = QPushButton("🗑 删除选中")
    del_done_btn.setObjectName("stopBtn")
    done_layout.addWidget(del_done_btn)
    tabs.addTab(done_widget, f"已完成 ({len(done)})")

    # ── 已忽略标签页 ──
    ignore_widget = QWidget()
    ignore_layout = QVBoxLayout(ignore_widget)
    ignore_layout.setContentsMargins(4, 4, 4, 4)
    ignore_layout.setSpacing(4)
    ignore_list = QListWidget()
    ignore_list.setSelectionMode(QAbstractItemView.ExtendedSelection)
    ignore_list.setFont(QFont("Consolas", 9))
    for p in ignored:
        item = QListWidgetItem(Path(p).name)
        item.setData(Qt.UserRole, p)
        ignore_list.addItem(item)
    ignore_layout.addWidget(ignore_list, 1)
    unignore_btn = QPushButton("↩ 恢复选中")
    unignore_btn.setObjectName("accentBtn")
    ignore_layout.addWidget(unignore_btn)
    tabs.addTab(ignore_widget, f"已忽略 ({len(ignored)})")

    # ── 底部关闭按钮 ──
    btn_row = QHBoxLayout()
    btn_row.setSpacing(4)
    close_btn = QPushButton("关闭")
    close_btn.clicked.connect(dlg.accept)
    btn_row.addStretch()
    btn_row.addWidget(close_btn)
    layout.addLayout(btn_row)

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
        data.pop("file_cost", None)
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
    path = Path(work_dir) / "cache" / ".subtitle_translation_cache.json"
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
        indices = {list_widget.row(item) for item in sel}
        # 必须同步删除进程级共享缓存：只改磁盘文件的话，内存里的共享缓存
        # 会在下次写盘时把被删条目原样写回（"删了又复活"）
        remove_shared_cache_entries(
            cache_keys[i] for i in indices if 0 <= i < len(cache_keys))
        fresh = shared_cache_snapshot()
        save_json(path, fresh)
        cache_keys = sorted(fresh.keys())
        # 从共享缓存快照整体重建列表（并发翻译新增的条目也会如实显示）
        list_widget.clear()
        for j, k in enumerate(cache_keys, 1):
            list_widget.addItem(f"{j:>4}. {fresh[k][:80]}")
        dlg.setWindowTitle(f"翻译缓存管理 ({list_widget.count()} 条)")
        size_after = path.stat().st_size if path.exists() else 0
        info.setText(f"缓存条目：{list_widget.count()} 条　　缓存大小：{size_after/1024:.1f} KB")
        log_callback(f"已从缓存中移除 {len(indices)} 条")

    del_btn.clicked.connect(_delete_selected)
    clear_btn.clicked.connect(lambda: _clear_all(dlg, path, info, list_widget, log_callback))
    dlg.exec()


def _clear_all(dlg, path, info_label, list_widget, log_callback):
    # 必须同步清空进程级共享缓存，否则内存条目会在下次写盘时复活
    clear_shared_cache()
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
    # glob.escape：stem 含 []?*（如 "movie [2024]"）时不被当作通配符导致匹配失真
    # 排除断点/中间产物：误选未完成的 .partial.srt 会把半截字幕嵌入并替换原视频
    for f in sorted(parent.glob(glob.escape(stem) + ".*.srt")):
        f_stem = f.stem
        if "partial" in f_stem.lower() or "translated" in f_stem.lower() \
                or "backup" in f_stem.lower() or "bak" in f_stem.lower():
            continue
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


class ExtractDialog(QDialog):
    """提取字幕对话框：选择含内嵌字幕的 MKV 文件，批量提取第一个字幕流为 SRT"""

    def __init__(self, parent, default_dir: str = ""):
        super().__init__(parent)
        self.setWindowTitle("📤 提取字幕")
        self.setMinimumSize(620, 420)
        self.resize(680, 480)
        self._default_dir = default_dir
        self._files = []  # [Path, ...]
        self._setup_ui()
        self._apply_style()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        title = QLabel("📤 提取字幕 — 从视频文件提取第一个字幕流为 SRT")
        title.setStyleSheet("font-size:14px; font-weight:600;")
        layout.addWidget(title)

        list_label = QLabel("待提取文件列表：")
        list_label.setStyleSheet("font-weight:600;")
        layout.addWidget(list_label)

        self.table = QListWidget()
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setMinimumHeight(150)
        layout.addWidget(self.table, 1)

        # ── 添加/移除按钮 ──
        btn_row = QHBoxLayout()
        add_btn = QPushButton("➕ 添加文件")
        add_btn.clicked.connect(self._browse_files)
        add_btn.setObjectName("accentBtn")
        btn_row.addWidget(add_btn)
        remove_btn = QPushButton("🗑 移除选中")
        remove_btn.clicked.connect(self._remove_selected)
        remove_btn.setObjectName("stopBtn")
        btn_row.addWidget(remove_btn)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        hint = QLabel("提示：仅提取第一个（默认）字幕流；图像字幕（PGS/VobSub 等）无法提取为文本 SRT。")
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#64748b; font-size:11px;")
        layout.addWidget(hint)

        # ── 转换选项（默认勾选：提取后转 MP4，便于播放器兼容）──
        self.convert_cb = QCheckBox("提取后转为 MP4（不带字幕，转换成功后删除原文件）")
        self.convert_cb.setChecked(True)
        layout.addWidget(self.convert_cb)

        # ── 底部按钮 ──
        bottom_row = QHBoxLayout()
        self.count_label = QLabel("共 0 个文件")
        self.count_label.setStyleSheet("color:#64748b;")
        bottom_row.addWidget(self.count_label)
        bottom_row.addStretch()
        self.start_btn = QPushButton("▶ 开始提取")
        self.start_btn.setObjectName("startBtn")
        self.start_btn.setEnabled(False)
        self.start_btn.clicked.connect(self.accept)
        self.start_btn.setFixedHeight(36)
        bottom_row.addWidget(self.start_btn)
        close_btn = QPushButton("✕ 关闭")
        close_btn.clicked.connect(self.reject)
        close_btn.setFixedHeight(36)
        bottom_row.addWidget(close_btn)
        layout.addLayout(bottom_row)

    def _browse_files(self):
        """浏览并多选视频文件（含内嵌字幕的 MKV 等）"""
        exts = " ".join(f"*{e}" for e in sorted(cfg.srt.video_exts))
        start = self._default_dir
        paths, _ = QFileDialog.getOpenFileNames(
            self, "选择视频文件", start, f"视频文件 ({exts})")
        if not paths:
            return
        for p in paths:
            pp = Path(p)
            if pp not in self._files:
                self._files.append(pp)
        self._refresh_table()

    def _remove_selected(self):
        rows = sorted({idx.row() for idx in self.table.selectedIndexes()}, reverse=True)
        for r in rows:
            if 0 <= r < len(self._files):
                del self._files[r]
        self._refresh_table()

    def _refresh_table(self):
        self.table.clear()
        for i, f in enumerate(self._files, 1):
            item = QListWidgetItem(f"{i}.  {f.name}")
            item.setToolTip(str(f))
            item.setData(Qt.UserRole, i - 1)
            self.table.addItem(item)
        count = len(self._files)
        self.count_label.setText(f"共 {count} 个文件")
        self.start_btn.setEnabled(count > 0)

    def get_files(self):
        """返回所有待提取的视频文件路径"""
        return self._files.copy()

    def should_convert_to_mp4(self) -> bool:
        """是否在提取后转为 MP4"""
        return self.convert_cb.isChecked()

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
        """)


def show_embed_confirm_dialog(parent, e) -> None:
    """翻译完成后、嵌入前暂停，弹出对话框让用户预览/编辑字幕。

    从 qt_app.py 抽取；通过 PauseResponse（translator.PauseResponse）与
    工作线程通信：确认后设置 resp.action，关闭时默认「跳过嵌入」。
    """
    text = e.get("text", "")
    file_name = e.get("file_name", "")
    resp = e.get("response")
    if resp is None:
        return

    dialog = QDialog(parent)
    dialog.setWindowTitle(f"确认嵌入字幕 — {file_name}")
    dialog.setMinimumSize(600, 500)
    dialog.resize(720, 580)

    layout = QVBoxLayout(dialog)

    info_label = QLabel(
        f"📄 <b>{file_name}</b> — 翻译完成，请确认字幕内容后点击「嵌入」或「跳过」"
    )
    info_label.setWordWrap(True)
    layout.addWidget(info_label)

    editor = QTextEdit()
    editor.setPlainText(text)
    editor.setFont(QFont("Consolas", 10))
    layout.addWidget(editor, 1)

    btn_layout = QHBoxLayout()
    btn_layout.addStretch()

    skip_btn = QPushButton("⏭ 跳过嵌入（仅保留外挂 SRT）")
    skip_btn.setToolTip("不嵌入字幕，仅保留独立的 SRT 文件")
    skip_btn.clicked.connect(lambda: _finish_pause("skip"))
    btn_layout.addWidget(skip_btn)

    embed_btn = QPushButton("✅ 确认嵌入")
    embed_btn.setObjectName("startBtn")
    embed_btn.setToolTip("将当前字幕嵌入 MKV 视频文件")
    embed_btn.setDefault(True)
    embed_btn.clicked.connect(lambda: _finish_pause("embed"))
    btn_layout.addWidget(embed_btn)

    layout.addLayout(btn_layout)

    def _finish_pause(action: str):
        resp.action = action
        if action == "embed":
            modified = editor.toPlainText()
            if modified != text:
                resp.modified_text = modified
        resp.event.set()
        dialog.accept()

    # 用户点击 X 关闭对话框时，默认跳过嵌入
    dialog.rejected.connect(lambda: _finish_pause("skip"))

    dialog.exec()

