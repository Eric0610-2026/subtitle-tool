#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PySide6/Qt 版主应用窗口
"""
import logging
import os, queue, re, subprocess, time, traceback
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import List, Optional, Dict

logger = logging.getLogger(__name__)

from PySide6.QtCore import Qt, QTimer, QEvent
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLineEdit, QCheckBox, QPushButton, QListWidget, QListWidgetItem,
    QTextEdit, QProgressBar, QLabel, QTabWidget, QSplitter, QGroupBox,
    QFrame, QFileDialog, QMessageBox, QSizePolicy, QAbstractItemView,
    QInputDialog,
)
from PySide6.QtGui import QFont, QColor, QFontMetrics

from .srt_utils import (
    SUB_EXTS, fmt_job_display, fmt_duration,
    load_json, save_json, estimate_eta,
    seconds_to_srt_time, srt_time_to_seconds, parse_srt,
    OverallProgress, find_tool,
)
from .pipeline import SubtitleWorker
from .config import cfg
from .dialogs import SettingsDialog, show_history_dialog, show_cache_dialog
from .muxer import embed_subtitles_to_video
from .widgets import DropListWidget, LogEntry, is_audio_file, SCAN_VIDEO_EXTS, AUDIO_EXTS

APP_DIR = Path(__file__).resolve().parent.parent


# ─── 配色（从 config.json 读取）───
LIGHT = {k: v for k, v in cfg.theme.light.__dict__.items()}
DARK = {k: v for k, v in cfg.theme.dark.__dict__.items()}

class SubtitleApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("🎬 本地字幕生成工具")
        self.resize(cfg.app.window_width, cfg.app.window_height)
        self.setMinimumSize(cfg.app.window_min_width, cfg.app.window_min_height)

        self.dark_mode = self._detect_system_dark()
        self.colors = DARK if self.dark_mode else LIGHT

        self.work_dir = str(APP_DIR)
        self.video_jobs: List[Path] = []
        self.subtitle_jobs: List[Path] = []
        # 使用有界队列防止内存溢出（maxsize=2000）
        self.ui_queue: queue.Queue = queue.Queue(maxsize=2000)
        self._last_progress_update = 0
        self.worker = SubtitleWorker()
        self._start_time: Optional[float] = None
        self._last_output_dir: Optional[Path] = None  # 记录最后输出目录
        self._output_paths: List[str] = []  # 本轮所有输出文件路径
        self._stats: Dict[str, any] = {}  # 处理统计
        self._overall = None  # 跨文件总进度跟踪
        self._settings_path = Path.home() / ".subtitle_tool_settings.json"
        # 默认配置（来自 config.json）
        self.settings_data = {
            "model_dir": str(APP_DIR / cfg.whisper.model_dir) if (APP_DIR / cfg.whisper.model_dir).exists() else cfg.whisper.model_dir,
            "language": cfg.whisper.language,
            "device": cfg.whisper.device,
            "compute_type": cfg.whisper.compute_type,
            "extract_audio": cfg.whisper.extract_audio,
            "vad_filter": cfg.whisper.vad_filter,
            "target_lang": cfg.translation.target_lang,
            "translation_model": cfg.translation.model,
            "api_url": cfg.translation.api_url,
            "api_key": cfg.translation.api_key,
            "pipeline": cfg.translation.pipeline,
            "translation_only": False,
            "translation_batch_size": cfg.translation.batch_size,
        }
        self._build_ui()
        self._apply_style()

        # 定时器轮询队列
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._poll_queue)
        self._timer.start(cfg.app.poll_interval_ms)

        self._add_log_entry("应用就绪")
        self._restore_window_state()

        # 初始化设置对话框（第一次点击时创建）
        self.settings_dialog = None

        # 启动检查
        QTimer.singleShot(500, self._run_startup_checks)

    # ─── 构建 UI ───

    def _restore_window_state(self):
        try:
            s = load_json(self._settings_path, {})
            geo = s.get("window_geometry")
            if geo:
                self.restoreGeometry(bytes.fromhex(geo))
            state = s.get("window_state")
            if state:
                self.restoreState(bytes.fromhex(state))
        except Exception:
            pass

    def _save_window_state(self):
        s = load_json(self._settings_path, {})
        s["window_geometry"] = self.saveGeometry().hex()
        s["window_state"] = self.saveState().hex()
        save_json(self._settings_path, s)

    def _make_btn(self, text, cb=None, object_name=None, tooltip=None, stylesheet=None, fixed_size=None):
        b = QPushButton(text)
        if cb:
            b.clicked.connect(cb)
        if object_name:
            b.setObjectName(object_name)
        if tooltip:
            b.setToolTip(tooltip)
        if stylesheet:
            b.setStyleSheet(stylesheet)
        if fixed_size:
            b.setFixedSize(*fixed_size)
        return b

    def _build_header(self, main):
        header = QFrame()
        header.setFixedHeight(48)
        header.setObjectName("header")
        hl = QHBoxLayout(header)
        hl.setContentsMargins(16, 0, 12, 0)
        title = QLabel("🎬 本地字幕生成工具")
        title.setStyleSheet("color:white; font-size:15px; font-weight:700;")
        hl.addWidget(title)
        hl.addStretch()
        ver = QLabel("Whisper + AI 翻译")
        ver.setStyleSheet("color:#94a3b8; font-size:11px;")
        hl.addWidget(ver)
        hl.addSpacing(8)
        self.theme_btn = QPushButton("☀" if not self.dark_mode else "🌙")
        self.theme_btn.setFixedSize(32, 28)
        self.theme_btn.clicked.connect(self._toggle_theme)
        hl.addWidget(self.theme_btn)
        main.addWidget(header)

    def _build_progress_group(self, name, label_attr, bar_attr, detail_attr):
        group = QGroupBox(name)
        v = QVBoxLayout(group)
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
        setattr(self, label_attr, label)
        setattr(self, bar_attr, bar)
        setattr(self, detail_attr, detail)
        return group

    def _build_progress_area(self, bl):
        pg = QGroupBox("进度")
        pgl = QVBoxLayout(pg)
        self.overall_label = QLabel("总进度：等待中")
        self.overall_label.setStyleSheet("font-weight:600; color:#6366f1;")
        pgl.addWidget(self.overall_label)
        self.overall_progress = QProgressBar()
        self.overall_progress.setRange(0, 100)
        self.overall_progress.setValue(0)
        self.overall_progress.setFixedHeight(16)
        self.overall_progress.setTextVisible(True)
        self.overall_progress.setFormat("%p%")
        pgl.addWidget(self.overall_progress)
        top = QHBoxLayout()
        self.lang_label = QLabel("语言：auto")
        top.addWidget(self.lang_label)
        top.addStretch()
        self.counter_label = QLabel("已转写 0/0 | 已翻译 0/0 | 缓存 0")
        top.addWidget(self.counter_label)
        pgl.addLayout(top)
        dual = QVBoxLayout()
        dual.addWidget(self._build_progress_group("转写", "transcribe_label", "transcribe_progress", "transcribe_detail"), 1)
        dual.addWidget(self._build_progress_group("翻译", "translate_label", "translate_progress", "translate_detail"), 1)
        pgl.addLayout(dual)
        bot = QHBoxLayout()
        self.detail_label = QLabel("已用 --:-- | 剩余 --:-- | 预计 --")
        bot.addWidget(self.detail_label, 1)
        pgl.addLayout(bot)
        lg = QGroupBox("日志")
        lgl = QVBoxLayout(lg)
        self.log_list = QListWidget()
        self.log_list.setObjectName("logList")
        self.log_list.setMinimumHeight(60)
        self.log_list.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOn)
        self.log_list.installEventFilter(self)
        lgl.addWidget(self.log_list)
        bottom_splitter = QSplitter(Qt.Horizontal)
        bottom_splitter.setChildrenCollapsible(False)
        bottom_splitter.addWidget(pg)
        bottom_splitter.addWidget(lg)
        bottom_splitter.setSizes([520, 520])
        bl.addWidget(bottom_splitter, 1)

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        main = QVBoxLayout(central)
        main.setContentsMargins(0, 0, 0, 0)
        main.setSpacing(0)
        self._build_header(main)

        # ── 主体内容 ──
        body = QWidget()
        body.setContentsMargins(12, 8, 12, 8)
        bl = QVBoxLayout(body)
        bl.setSpacing(6)

        # ── 路径行 + 翻译开关 + 更多设置 ──
        pr = QHBoxLayout()
        pr.setSpacing(6)
        pr.addWidget(QLabel("视频目录"))
        self.video_dir = QLineEdit()
        self.video_dir.setPlaceholderText("选择视频目录...")
        pr.addWidget(self.video_dir, 1)
        pr.addWidget(self._make_btn("📂 选择", self._choose_video_dir))
        pr.addWidget(self._make_btn("📌 默认", self._set_default_video_dir))
        pr.addSpacing(12)
        self.trans_cb = QCheckBox("🌍 开启 AI 翻译")
        self.trans_cb.setChecked(True)
        pr.addWidget(self.trans_cb)
        pr.addWidget(self._make_btn("⚙ 更多设置", self._open_settings, object_name="accentBtn"))
        bl.addLayout(pr)

        # ── 中间双栏 ──
        splitter = QSplitter()

        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        self.tabs = QTabWidget()
        self.video_list = DropListWidget(is_video_tab=True)
        self.video_list.itemClicked.connect(lambda item: self._load_preview(item, True))
        self.video_list.dropped.connect(lambda paths, is_v: self._add_paths(paths, True))
        self.video_list.reordered.connect(lambda: self._sync_jobs_from_list(True))
        self.sub_list = DropListWidget(is_video_tab=False)
        self.sub_list.itemClicked.connect(lambda item: self._load_preview(item, False))
        self.sub_list.dropped.connect(lambda paths, is_v: self._add_paths(paths, False))
        self.sub_list.reordered.connect(lambda: self._sync_jobs_from_list(False))
        self.tabs.addTab(self.video_list, "视频/音频生成字幕")
        self.tabs.addTab(self.sub_list, "已有字幕翻译")
        ll.addWidget(self.tabs)

        btn_row = QHBoxLayout()
        for text, cb in [
            ("📂 添加文件", lambda: self._add_files(self.tabs.currentIndex() == 0)),
            ("📁 添加文件夹", lambda: self._add_folder(self.tabs.currentIndex() == 0)),
            ("🔍 扫描", lambda: self._scan_dir()),
            ("✕ 移除", lambda: self._remove_selected()),
            ("☑ 全选", lambda: self._select_all()),
            ("🗑 清空", lambda: self._clear_jobs()),
        ]:
            btn_row.addWidget(self._make_btn(text, cb, object_name="actionBtn"))
        ll.addLayout(btn_row)
        splitter.addWidget(left)

        right = QWidget()
        rl2 = QVBoxLayout(right)
        rl2.setContentsMargins(4, 0, 0, 0)
        preview_header = QHBoxLayout()
        preview_header.addWidget(QLabel("字幕预览"))
        preview_header.addWidget(self._make_btn("🔍 查找", self._find_in_preview, object_name="actionBtn"))
        preview_header.addWidget(self._make_btn("💾 保存修改", self._save_preview, object_name="actionBtn"))
        preview_header.addWidget(self._make_btn("⏱ 偏移", self._offset_preview_time, object_name="actionBtn",
                                       tooltip="批量调整字幕时间戳（±秒）"))
        preview_header.addStretch()
        rl2.addLayout(preview_header)
        self.preview = QTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setFont(QFont("Consolas", 10))
        rl2.addWidget(self.preview, 1)
        splitter.addWidget(right)
        splitter.setSizes([600, 600])

        bl.addWidget(splitter, 2)
        self._build_progress_area(bl)

        main.addWidget(body, 1)

        # ── 操作按钮 ──
        ar = QHBoxLayout()
        ar.setContentsMargins(12, 4, 12, 6)
        self.start_btn = self._make_btn("▶ 开始处理", self._start, object_name="startBtn")
        ar.addWidget(self.start_btn)
        self.stop_btn = self._make_btn("⏹ 停止", self._stop, object_name="stopBtn")
        self.stop_btn.setEnabled(False)
        ar.addWidget(self.stop_btn)
        ar.addWidget(self._make_btn("🔄 重试", self._retry, object_name="bottomBtn",
                           stylesheet=f"QPushButton {{ background:{self.colors['accent']}; color:white; border:none; }} "
                                      "QPushButton:hover { background:#4f46e5; }"))
        ar.addWidget(self._make_btn("📋 历史", self._show_history, object_name="bottomBtn"))
        ar.addWidget(self._make_btn("💾 缓存", self._show_cache, object_name="bottomBtn"))
        ar.addWidget(self._make_btn("📦 嵌入字幕", self._manual_embed, object_name="bottomBtn"))
        ar.addWidget(self._make_btn("📤 导出日志", self._export_log, object_name="bottomBtn"))
        ar.addStretch()
        main.addLayout(ar)

    # ─── 样式 ───

    def _apply_style(self):
        c = self.colors
        border_radius = "border-radius:6px;"
        self.setStyleSheet(f"""
            QMainWindow {{ background: {c['bg']}; }}
            QWidget {{ background: {c['bg']}; color: {c['text']}; font-size: 13px; }}
            QFrame#header {{ background: {c['header']}; border: none; }}
            QFrame#card {{ background: {c['card']}; {border_radius} border:1px solid {c['border']}; }}
            QGroupBox {{ background: {c['card']}; {border_radius} border:1px solid {c['border']}; margin-top:10px; padding:10px; font-weight:600; color:{c['accent']}; }}
            QGroupBox::title {{ subcontrol-origin:margin; left:10px; padding:0 5px; }}
            QLineEdit, QComboBox, QTextEdit, QListWidget {{ background:{c['card']}; color:{c['text']}; border:1px solid {c['border']}; {border_radius} padding:4px 6px; }}
            QComboBox::drop-down {{ border:none; }}
            QPushButton {{ background:{c['card']}; color:{c['text']}; border:1px solid {c['border']}; {border_radius} padding:6px 14px; }}
            QPushButton:hover {{ background:{c['border']}; }}
            QPushButton#bottomBtn {{ padding:8px 16px; font-size:13px; font-weight:600; }}
            QPushButton#startBtn {{ background:{c['success']}; color:white; border:none; font-weight:bold; padding:8px 20px; font-size:13px; }}
            QPushButton#startBtn:hover {{ background:#16a34a; }}
            QPushButton#startBtn:disabled {{ background:{c['text_muted']}; }}
            QPushButton#stopBtn {{ background:{c['danger']}; color:white; border:none; font-weight:bold; padding:8px 20px; font-size:13px; }}
            QPushButton#stopBtn:hover {{ background:#dc2626; }}
            QPushButton#stopBtn:disabled {{ background:{c['text_muted']}; }}
            QPushButton#accentBtn {{ background:{c['accent']}; color:white; border:none; padding:6px 14px; }}
            QPushButton#accentBtn:hover {{ background:#4f46e5; }}
            QPushButton#actionBtn {{ padding:5px 10px; font-size:12px; }}
            QProgressBar {{ background:{c['border']}; border:none; {border_radius} }}
            QProgressBar::chunk {{ background: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 {c['accent']}, stop:1 #818cf8); {border_radius} }}
            QTabWidget::pane {{ background:{c['card']}; border:1px solid {c['border']}; }}
            QTabBar::tab {{ background:{c['border']}; color:{c['text_sec']}; padding:6px 16px; }}
            QTabBar::tab:selected {{ background:{c['card']}; color:{c['accent']}; }}
            QCheckBox {{ spacing:4px; }}
            QScrollBar:vertical {{ width:8px; background:{c['bg']}; border:none; }}
            QScrollBar::handle:vertical {{ background:{c['border']}; {border_radius} min-height:24px; }}
            QScrollBar::handle:vertical:hover {{ background:{c['text_muted']}; }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height:0; border:none; }}
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background:none; }}
            QScrollBar:horizontal {{ height:8px; background:{c['bg']}; border:none; }}
            QScrollBar::handle:horizontal {{ background:{c['border']}; {border_radius} min-width:24px; }}
            QScrollBar::handle:horizontal:hover {{ background:{c['text_muted']}; }}
            QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width:0; border:none; }}
            QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {{ background:none; }}
            QSplitter::handle {{ background:{c['border']}; }}
            QSplitter::handle:horizontal {{ width:1px; }}
            QSplitter::handle:vertical {{ height:3px; }}
            QLabel {{ background:transparent; }}
            QListWidget#logList {{ background:{c['card']}; border:1px solid {c['border']}; }}
            QListWidget#logList::item {{ padding:0; }}
        """)

    # ─── 交互 ───

    def _choose_video_dir(self):
        d = QFileDialog.getExistingDirectory(self, "选择视频目录", self.video_dir.text())
        if d:
            self.video_dir.setText(d)
            self._scan_path(d, True)

    def _set_default_video_dir(self):
        self.video_dir.setText(cfg.app.default_video_dir)
        self._add_log_entry(f"视频目录已设为 {cfg.app.default_video_dir}")
        self._scan_path(cfg.app.default_video_dir, True)

    def _open_settings(self):
        dlg = SettingsDialog(self, self.settings_data)
        result = dlg.exec()
        if result == 1:
            self.settings_data = dlg.get_values()
            self._add_log_entry("设置已应用（本次运行有效）")
        elif result == 2:
            self.settings_data = dlg.get_values()
            self._save_settings_permanently(dlg.get_values())
            self._add_log_entry("设置已保存到 config.json（永久生效）")

    def _save_settings_permanently(self, values: dict):
        import json
        path = Path(__file__).resolve().parent / "config.json"
        if not path.exists():
            QMessageBox.warning(self, "保存失败", "未找到 config.json，请先复制 config.example.json")
            return
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw.setdefault("whisper", {})["model_dir"] = values.get("model_dir", "")
        raw["whisper"]["language"] = values.get("language", "auto")
        raw["whisper"]["device"] = values.get("device", "cuda")
        raw["whisper"]["compute_type"] = values.get("compute_type", "int8_float16")
        raw["whisper"]["extract_audio"] = values.get("extract_audio", True)
        raw["whisper"]["vad_filter"] = values.get("vad_filter", True)
        raw.setdefault("translation", {})["target_lang"] = values.get("target_lang", "zh")
        raw["translation"]["model"] = values.get("translation_model", "")
        raw["translation"]["api_url"] = values.get("api_url", "")
        raw["translation"]["api_key"] = values.get("api_key", "")
        raw["translation"]["pipeline"] = values.get("pipeline", True)
        raw["translation"]["batch_size"] = values.get("translation_batch_size", 50)
        path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
 

    def _add_files(self, is_video: bool):
        exts = SCAN_VIDEO_EXTS | AUDIO_EXTS if is_video else SUB_EXTS
        ext_str = " ".join(f"*{e}" for e in sorted(exts))
        files, _ = QFileDialog.getOpenFileNames(
            self, "选择文件", self.video_dir.text(),
            f"媒体文件 ({ext_str})")
        if files:
            self._add_paths([Path(f) for f in files], is_video)

    def _add_folder(self, is_video: bool):
        d = QFileDialog.getExistingDirectory(self, "选择文件夹", self.video_dir.text())
        if d:
            exts = SCAN_VIDEO_EXTS | AUDIO_EXTS if is_video else SUB_EXTS
            paths = []
            for f in sorted(Path(d).iterdir()):
                if f.suffix.lower() in exts:
                    paths.append(f)
            self._add_paths(paths, is_video)
            self._add_log_entry(f"已扫描文件夹：{d}")

    def _load_done_set(self):
        done = set()
        done_stems = set()
        prog_path = APP_DIR / ".subtitle_progress.json"
        if prog_path.exists():
            data = load_json(prog_path, {})
            for path_str in data.get("done", []):
                done.add(path_str)
                done_stems.add(Path(path_str).stem)
                first_part = Path(path_str).stem.split(".")[0]
                if first_part != Path(path_str).stem:
                    done_stems.add(first_part)
            if done:
                self._add_log_entry(f"历史记录：{len(done)} 个已完成文件")
        return done, done_stems

    def _add_paths(self, paths: List[Path], is_video: bool):
        lb = self.video_list if is_video else self.sub_list
        jobs = self.video_jobs if is_video else self.subtitle_jobs
        exts = SCAN_VIDEO_EXTS | AUDIO_EXTS if is_video else SUB_EXTS
        existing = {str(p.resolve()) for p in jobs}
        done, done_stems = self._load_done_set()
        added = 0
        skipped = 0
        for p in paths:
            if p.suffix.lower() not in exts:
                continue
            resolved = str(p.resolve())
            if resolved in existing:
                continue
            if resolved in done or p.stem in done_stems or str(p) in done:
                skipped += 1
                continue
            jobs.append(p)
            existing.add(resolved)
            item = QListWidgetItem(fmt_job_display(p))
            item.setData(Qt.UserRole, str(p))
            lb.addItem(item)
            added += 1
        if added:
            self._add_log_entry(f"已添加 {added} 个文件" + (f"，{skipped} 个已完成已跳过" if skipped else ""))
        elif skipped:
            self._add_log_entry(f"无新文件，{skipped} 个已完成已跳过")

    def _scan_path(self, path: str, is_video: bool):
        d = Path(path)
        if not d.exists():
            return
        exts = SCAN_VIDEO_EXTS | AUDIO_EXTS if is_video else SUB_EXTS
        self._add_paths([d / f for f in sorted(d.iterdir()) if f.suffix.lower() in exts], is_video)

    def _scan_dir(self):
        is_video = self.tabs.currentIndex() == 0
        path = self.video_dir.text() if is_video else self.work_dir
        self._scan_path(path, is_video)
        if not is_video and not self.video_dir.text():
            self._scan_path(self.work_dir, False)

    def _remove_selected(self):
        is_video = self.tabs.currentIndex() == 0
        lb = self.video_list if is_video else self.sub_list
        jobs = self.video_jobs if is_video else self.subtitle_jobs
        for item in reversed(sorted(lb.selectedItems(), key=lambda x: lb.row(x))):
            row = lb.row(item)
            if 0 <= row < len(jobs):
                jobs.pop(row)
            lb.takeItem(row)
        self._add_log_entry("已移除选中项")

    def _sync_jobs_from_list(self, is_video: bool):
        """列表拖拽排序后，按新顺序重建 jobs"""
        lb = self.video_list if is_video else self.sub_list
        jobs = self.video_jobs if is_video else self.subtitle_jobs
        new_jobs = []
        for i in range(lb.count()):
            path_str = lb.item(i).data(Qt.UserRole)
            if path_str:
                new_jobs.append(Path(path_str))
        jobs.clear()
        jobs.extend(new_jobs)

    def _select_all(self):
        lb = self.video_list if self.tabs.currentIndex() == 0 else self.sub_list
        lb.selectAll()

    def _confirm(self, title, text, default_no=False):
        box = QMessageBox(self)
        box.setWindowTitle(title)
        box.setText(text)
        box.setIcon(QMessageBox.NoIcon)
        box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
        if default_no:
            box.setDefaultButton(QMessageBox.No)
        return box.exec() == QMessageBox.Yes

    def _clear_jobs(self):
        if not self._confirm("清空队列", "确定清空当前文件列表？"):
            return
        is_video = self.tabs.currentIndex() == 0
        lb = self.video_list if is_video else self.sub_list
        jobs = self.video_jobs if is_video else self.subtitle_jobs
        lb.clear()
        jobs.clear()
        self._add_log_entry("队列已清空")

    def _load_preview(self, item, is_video: bool):
        """选中文件时加载对应 SRT 到预览区"""
        jobs = self.video_jobs if is_video else self.subtitle_jobs
        lb = self.video_list if is_video else self.sub_list
        row = lb.row(item)
        if row < 0 or row >= len(jobs):
            return
        stem = jobs[row].stem
        parent = jobs[row].parent
        candidates = [
            parent / stem / f"{stem}.srt",
            parent / f"{stem}.srt",
        ]
        for f in sorted(parent.iterdir()):
            if f.suffix == ".srt" and f.stem.startswith(stem):
                candidates.append(f)
        sub_dir = parent / stem
        if sub_dir.exists():
            for f in sorted(sub_dir.iterdir()):
                if f.suffix == ".srt" and f.stem.startswith(stem):
                    candidates.append(f)
        for c in candidates:
            if c.exists():
                self.preview.setReadOnly(False)
                self.preview.setText(c.read_text(encoding="utf-8"))
                self._last_output_dir = c.parent
                return
        self.preview.setReadOnly(True)

    def _find_in_preview(self):
        """在预览区弹出查找对话框"""
        text, ok = QInputDialog.getText(self, "查找", "输入要查找的文本：")
        if not ok or not text:
            return
        content = self.preview.toPlainText()
        # 先清除上次高亮
        fmt_normal = self.preview.currentCharFormat()
        cursor = self.preview.textCursor()
        cursor.select(cursor.SelectionType.Document)
        cursor.setCharFormat(fmt_normal)
        self.preview.setTextCursor(cursor)
        # 查找并高亮
        found = False
        cursor = self.preview.textCursor()
        cursor.movePosition(cursor.MoveOperation.Start)
        fmt = cursor.charFormat()
        fmt.setBackground(QColor("#fbbf24"))
        pos = 0
        while True:
            idx = content.find(text, pos)
            if idx == -1:
                break
            found = True
            cursor.setPosition(idx)
            cursor.setPosition(idx + len(text), cursor.MoveMode.KeepAnchor)
            cursor.setCharFormat(fmt)
            pos = idx + len(text)
        if found:
            self._add_log_entry(f"预览区查找完成：{text}")
        else:
            self._add_log_entry(f"预览区未找到：{text}")

    def _offset_preview_time(self):
        """批量调整预览区字幕时间戳"""
        content = self.preview.toPlainText().strip()
        if not content:
            QMessageBox.information(self, "提示", "预览区为空")
            return
        offset, ok = QInputDialog.getDouble(self, "时间偏移",
                                              "偏移量（秒）：正数=延后，负数=提前", 0, -3600, 3600, 1)
        if not ok:
            return
        # 匹配所有 SRT 时间戳行：HH:MM:SS,mmm --> HH:MM:SS,mmm
        ts_re = re.compile(r"(\d+:\d{1,2}:\d{1,2}[,.]\d{1,3})\s*-->\s*(\d+:\d{1,2}:\d{1,2}[,.]\d{1,3})")

        def _shift(m):
            start = max(0, srt_time_to_seconds(m.group(1)) + offset)
            end = max(0, srt_time_to_seconds(m.group(2)) + offset)
            return f"{seconds_to_srt_time(start)} --> {seconds_to_srt_time(end)}"

        new_content = ts_re.sub(_shift, content)
        self.preview.setText(new_content)
        self._add_log_entry(f"时间偏移 {offset:+.1f}s（预览区）")
        # 自动保存
        self._save_preview()

    def _save_preview(self):
        """保存预览区修改到当前任务的 SRT"""
        is_video = self.tabs.currentIndex() == 0
        jobs = self.video_jobs if is_video else self.subtitle_jobs
        lb = self.video_list if is_video else self.sub_list
        sel = lb.selectedItems()
        if not sel or not jobs:
            QMessageBox.information(self, "提示", "请先选中一个文件")
            return
        row = lb.row(sel[0])
        if row < 0 or row >= len(jobs):
            return
        stem = jobs[row].stem
        output_dir = jobs[row].parent / stem
        srt_path = output_dir / f"{stem}.srt"
        if not srt_path.exists():
            srt_path = jobs[row].parent / f"{stem}.srt"
        try:
            srt_path.write_text(self.preview.toPlainText(), encoding="utf-8")
            self._add_log_entry(f"已保存预览修改：{srt_path.name}")
        except Exception as e:
            QMessageBox.warning(self, "保存失败", str(e))

    # ─── 处理控制 ───

    def _build_opts(self, skip_completed=False):
        s = self.settings_data
        return {
            "work_dir": self.work_dir,
            "model_dir": s.get("model_dir", ""),
            "language": s.get("language", "auto"),
            "target_lang": s.get("target_lang", "zh"),
            "device": s.get("device", "cuda"),
            "compute_type": s.get("compute_type", "int8_float16"),
            "translate_enabled": self.trans_cb.isChecked(),
            "extract_audio": s.get("extract_audio", True),
            "vad_filter": s.get("vad_filter", True),
            "api_url": s.get("api_url", ""),
            "api_key": s.get("api_key", ""),
            "translation_model": s.get("translation_model", ""),
            "translation_only": s.get("translation_only", False),
            "translation_batch_size": s.get("translation_batch_size", cfg.translation.batch_size),
            "skip_completed": skip_completed,
            "concurrency": cfg.translation.concurrency_pipeline if s.get("pipeline", True) else cfg.translation.concurrency_serial,
            "post": self.ui_queue.put,
            "_is_stopped": lambda: self.worker.stop_requested,
            "_register_proc": self.worker._register_proc,
            "_unregister_proc": self.worker._unregister_proc,
        }

    def _begin_processing(self, jobs, opts, log_msg):
        self._start_time = time.time()
        self._stats = {"files": len(jobs), "cache_hits": 0, "translated_segments": 0,
                       "transcribed_segments": 0, "start": self._start_time}
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self._reset_progress()
        self.preview.setReadOnly(True)
        self.preview.clear()
        self._add_log_entry(log_msg)
        w = getattr(getattr(cfg, "progress", None) or type("", (), {})(), "transcribe_weight", 80.0)
        self._overall = OverallProgress(len(jobs), transcribe_weight=w)
        self._overall.start()
        self.overall_progress.setValue(0)
        self.overall_label.setText(f"总进度：第 1/{len(jobs)} 个 · 已完成 0% · 等待中")
        self.worker.start(jobs, opts)

    def _get_jobs(self):
        is_video = self.tabs.currentIndex() == 0
        return self.video_jobs if is_video else self.subtitle_jobs

    def _start(self):
        if self.worker.thread and self.worker.thread.is_alive():
            QMessageBox.warning(self, "提示", "正在处理中")
            return
        jobs = self._get_jobs()
        if not jobs:
            QMessageBox.warning(self, "提示", "队列为空")
            return
        self._begin_processing(jobs, self._build_opts(False),
                               f"开始处理，队列 {len(jobs)} 个文件")

    def _set_elided(self, label: QLabel, text: str) -> None:
        fm = QFontMetrics(label.font())
        w = max(label.width(), 200)
        label.setText(fm.elidedText(text, Qt.ElideRight, w))

    def _reset_progress(self):
        self.transcribe_progress.setValue(0)
        self.transcribe_label.setText("等待中")
        self.transcribe_detail.setText("")
        self.translate_progress.setValue(0)
        self.translate_label.setText("等待中")
        self.translate_detail.setText("")
        self.overall_progress.setValue(0)
        self.overall_label.setText("总进度：等待中")

    def _stop(self):
        if not (self.worker.thread and self.worker.thread.is_alive()):
            return
        if not self._confirm("停止确认", "确定要停止当前处理吗？\n已完成处理的文件不会丢失。", default_no=True):
            return
        self.worker.stop()
        self._add_log_entry("已请求停止")

    def _retry(self):
        if self.worker.thread and self.worker.thread.is_alive():
            QMessageBox.warning(self, "提示", "正在处理中")
            return
        jobs = self._get_jobs()
        if not jobs:
            QMessageBox.warning(self, "提示", "队列为空")
            return
        self._begin_processing(jobs, self._build_opts(True),
                               f"断点续翻，检查 {len(jobs)} 个文件...")

    def _show_history(self):
        try:
            show_history_dialog(self, self.work_dir, self._add_log_entry)
        except Exception as e:
            self._add_log_entry(f"打开历史对话框失败: {e}", level="ERROR", trace=traceback.format_exc())

    def _show_cache(self):
        try:
            show_cache_dialog(self, self.work_dir, self._add_log_entry)
        except Exception as e:
            self._add_log_entry(f"打开缓存对话框失败: {e}", level="ERROR", trace=traceback.format_exc())

    # ─── 手动嵌入 ───

    def _manual_embed(self):
        """手动选择视频与字幕文件，软内嵌为 MKV"""
        ffmpeg = find_tool("ffmpeg.exe", APP_DIR) or find_tool("ffmpeg", APP_DIR)
        if not ffmpeg:
            QMessageBox.warning(self, "错误", "未找到 ffmpeg，请放在应用目录下")
            return

        video_exts = " ".join(f"*{e}" for e in cfg.srt.video_exts)
        video_str, _ = QFileDialog.getOpenFileName(
            self, "选择视频文件", self.video_dir.text(), f"视频文件 ({video_exts})")
        if not video_str:
            return
        video = Path(video_str)

        srt_str, _ = QFileDialog.getOpenFileName(
            self, "选择字幕文件(SRT)", str(video.parent), "字幕文件 (*.srt)")
        if not srt_str:
            return
        srt = Path(srt_str)

        if not video.exists() or not srt.exists():
            return

        if not self._confirm("确认嵌入",
                f"视频: {video.name}\n字幕: {srt.name}\n输出: {video.with_suffix('.mkv').name}\n\n确定要嵌入？"):
            return

        def post(msg):
            if msg.get("type") == "log":
                self._add_log_entry(msg.get("message", ""), msg.get("level", "INFO"))

        self._add_log_entry(f"📦 手动嵌入: {video.name} + {srt.name}")
        QApplication.processEvents()
        mkv, _ = embed_subtitles_to_video(video, srt, ffmpeg, post)
        if mkv and mkv.exists():
            self._add_log_entry(f"✅ 手动嵌入完成: {mkv.name}")
            if str(mkv.resolve()) not in self._output_paths:
                self._output_paths.append(str(mkv.resolve()))
            try:
                video.unlink()
                srt.unlink()
                self._add_log_entry(f"已删除原文件: {video.name}, {srt.name}")
            except OSError as e:
                self._add_log_entry(f"删除原文件失败: {e}", "WARNING")
        else:
            self._add_log_entry("❌ 手动嵌入失败", "WARNING")

    def _add_log_entry(self, message: str, level: str = "INFO", trace: str = None) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        text = f"[{ts}] {message}"
        item = QListWidgetItem()
        entry = LogEntry(text, level, trace)
        item.setSizeHint(entry.sizeHint())
        self.log_list.addItem(item)
        self.log_list.setItemWidget(item, entry)
        entry._list_item = item
        # 下一布局周期后按真实视口宽度重算该条目高度，避免初始宽度未定导致高度偏差
        QTimer.singleShot(0, lambda: item.setSizeHint(entry.sizeHint()))
        max_lines = cfg.app.max_log_lines
        while self.log_list.count() > max_lines:
            self.log_list.takeItem(0)

    def _run_startup_checks(self):
        missing_essential = []
        if not find_tool("ffmpeg.exe", APP_DIR) and not find_tool("ffmpeg", APP_DIR):
            missing_essential.append("ffmpeg.exe")
            self._add_log_entry("未找到 ffmpeg.exe，请放入应用目录", "WARNING")
        if not find_tool("ffprobe.exe", APP_DIR) and not find_tool("ffprobe", APP_DIR):
            missing_essential.append("ffprobe.exe")
            self._add_log_entry("未找到 ffprobe.exe，请放入应用目录（与 ffmpeg 在同一目录）", "WARNING")
        if missing_essential:
            QMessageBox.warning(self, "缺少必需文件",
                f"未找到 {', '.join(missing_essential)}，请放入项目根目录后重启应用。")
        model_dir = APP_DIR / "faster-whisper-large-v3-turbo"
        if not model_dir.is_dir() or not (model_dir / "model.bin").is_file():
            self._add_log_entry("未找到 faster-whisper 模型，请下载后放入 faster-whisper-large-v3-turbo/ 目录（下载地址：https://www.modelscope.cn/models/pengzhendong/faster-whisper-large-v3-turbo/summary）", "WARNING")
        s = self.settings_data
        if not s.get("api_url") or not s.get("api_key"):
            self._add_log_entry("API 地址或密钥未设置，请在设置中配置后使用翻译功能", "WARNING")

    def _export_log(self):
        """导出当前日志列表到文件"""
        if self.log_list.count() == 0:
            QMessageBox.information(self, "导出日志", "日志为空，无需导出。")
            return

        lines = []
        for i in range(self.log_list.count()):
            item = self.log_list.item(i)
            w = self.log_list.itemWidget(item)
            if w is not None and hasattr(w, "message"):
                level = getattr(w, "level", "INFO").ljust(5)
                line = f"{w.message}"
                lines.append(line)
                if getattr(w, "trace", None):
                    for tl in w.trace.rstrip().split("\n"):
                        lines.append(f"  {tl}")
            else:
                # fallback: 从 item text 提取
                lines.append(item.text())

        header = (
            f"本地字幕生成工具 - 日志导出\n"
            f"导出时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"日志条数: {len(lines)}\n"
            f"{'=' * 60}\n\n"
        )
        content = header + "\n".join(lines)

        default_name = f"subtitle_log_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
        path, _ = QFileDialog.getSaveFileName(
            self, "导出日志", default_name,
            "文本文件 (*.txt);;所有文件 (*.*)")
        if not path:
            return

        try:
            Path(path).write_text(content, encoding="utf-8")
            self._add_log_entry(f"日志已导出：{path}")
            QMessageBox.information(self, "导出成功", f"已导出 {len(lines)} 条日志到：\n{path}")
        except Exception as e:
            logger.error("导出日志失败: %s", e)
            QMessageBox.warning(self, "导出失败", f"导出日志失败：{e}")

    # ─── 轮询队列 ───

    def _poll_queue(self):
        try:
            while True:
                self._handle_event(self.ui_queue.get_nowait())
        except queue.Empty:
            pass

    def _handle_progress(self, e):
        pct = e.get("percent", 0)
        stage = e.get("stage", "")
        detail = e.get("detail", "")
        if stage in ("提取音频", "加载模型", "读取字幕", "转写中"):
            self.transcribe_progress.setValue(int(pct))
            self.transcribe_progress.setFormat(f"{int(pct)}%")
            self.transcribe_detail.setText(detail)
        elif stage == "翻译":
            self.translate_progress.setValue(int(pct))
            self.translate_progress.setFormat(f"{int(pct)}%")
            self.translate_detail.setText(detail)
        elif stage == "组织输出":
            self.transcribe_progress.setValue(100)
            self.transcribe_progress.setFormat("100%")
            self.translate_progress.setValue(100)
            self.translate_progress.setFormat("100%")
        _has_detail_above = stage in ("提取音频", "加载模型", "读取字幕", "转写中", "翻译")
        if not _has_detail_above and detail and self._start_time and pct:
            elapsed = time.time() - self._start_time
            remain, finish = estimate_eta(self._start_time, pct / 100)
            self.detail_label.setText(f"{detail} | 已用 {fmt_duration(elapsed)} | 剩余 {remain} | 预计 {finish}")
        elif not _has_detail_above and detail:
            self.detail_label.setText(detail)
        elif self._start_time and pct:
            elapsed = time.time() - self._start_time
            remain, finish = estimate_eta(self._start_time, pct / 100)
            self.detail_label.setText(f"已用 {fmt_duration(elapsed)} | 剩余 {remain} | 预计 {finish}")
        else:
            self.detail_label.setText("")
        idx = e.get("idx", 0)
        if idx and self._overall is not None:
            overall_pct = self._overall.tick(idx, pct, stage)
            self.overall_progress.setValue(int(overall_pct))
            remain, finish = self._overall.eta()
            self.overall_label.setText(
                f"总进度：第 {idx}/{self._overall.total} 个 · 已完成 {overall_pct:.0f}% · "
                f"预计全部完成 {finish}（剩余 {remain}）")

    def _handle_done(self, e):
        msg = e.get("message", "完成")
        self._add_log_entry(msg, "INFO")
        self.transcribe_progress.setValue(100)
        self.transcribe_progress.setFormat("100%")
        self.translate_progress.setValue(100)
        self.translate_progress.setFormat("100%")
        self.detail_label.setText("")
        if self._overall is not None:
            self._overall.set_complete()
            self.overall_progress.setValue(100)
            self.overall_label.setText("总进度：全部完成 100%")
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.preview.setReadOnly(False)
        elapsed = time.time() - self._start_time if self._start_time else 0
        stats_msg = f"处理完成 | 总耗时 {fmt_duration(elapsed)} | {self._stats.get('files', 0)} 个文件"
        self._add_log_entry(stats_msg)
        self._notify("字幕工具", f"{msg}\n{stats_msg}")

    def _handle_error(self, e):
        msg = e.get("message", "错误")
        self._add_log_entry(msg, "ERROR", trace=e.get("trace", ""))
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self._reset_progress()
        self.preview.setReadOnly(False)

    def _handle_event(self, event: dict):
        t = event.get("type", "")
        handlers = {
            "log": lambda e: self._add_log_entry(e.get("message", ""), e.get("level", "INFO")),
            "transcribe_status": lambda e: self._set_elided(self.transcribe_label,
                f"🎤 {e.get('file','')} [{e.get('idx',0)}/{e.get('total',0)}]"),
            "file_mode": lambda e: self._overall.set_file_translation_only(e.get("idx", 0))
                if self._overall and not e.get("needs_transcribe", True) else None,
            "translate_status": lambda e: self._set_elided(self.translate_label,
                f"🌍 {e.get('file','')} [{e.get('idx',0)}/{e.get('total',0)}]"),
            "current": lambda e: self._set_elided(self.transcribe_label, f"🎤 {e.get('message', '')}"),
            "progress": self._handle_progress,
            "counter": lambda e: self.counter_label.setText(
                f"已转写 {e.get('generated',0)}/{e.get('total',0)} | "
                f"已翻译 {e.get('translated',0)}/{e.get('total',0)} | "
                f"缓存 {e.get('cache',0)}"),
            "language": lambda e: self.lang_label.setText(f"语言：{e.get('message','')}"),
            "output_path": self._handle_output_path,
            "preview": lambda e: self.preview.setText(e.get("message", "")),
            "preview_clear": lambda e: self.preview.clear(),
            "preview_append": self._handle_preview_append,
            "done": self._handle_done,
            "error": self._handle_error,
        }
        handler = handlers.get(t)
        if handler:
            handler(event)

    def _handle_output_path(self, e):
        p = Path(e.get("path", ""))
        self._output_paths.append(str(p))
        self._last_output_dir = p.parent
        self._check_subtitle_quality(p)

    def _handle_preview_append(self, e):
        self.preview.append(e.get("message", ""))
        self.preview.verticalScrollBar().setValue(self.preview.verticalScrollBar().maximum())

    def _check_subtitle_quality(self, path: Path):
        """检查字幕质量问题"""
        if not path.suffix == ".srt" or not path.exists():
            return
        try:
            blocks = parse_srt(path)
        except Exception:
            return
        issues = []
        for i, b in enumerate(blocks):
            dur = b.end - b.start
            if dur < 0.3 and b.text.strip():
                issues.append(f"  #{b.index} 时长过短 ({dur:.1f}s): {b.text[:40]}")
            if dur > 15:
                issues.append(f"  #{b.index} 时长过长 ({dur:.1f}s): {b.text[:40]}")
            if i > 0:
                prev_end = blocks[i - 1].end
                if b.start < prev_end:
                    issues.append(f"  #{blocks[i-1].index}->#{b.index} 时间重叠 ({prev_end:.1f}s->{b.start:.1f}s)")
                gap = b.start - prev_end
                if gap > 10:
                    issues.append(f"  #{blocks[i-1].index}->#{b.index} 间隙过长 ({gap:.1f}s)")
        if issues:
            self._add_log_entry(f"⚠ 字幕质量提醒 ({path.name}):")
            for issue in issues[:10]:
                self._add_log_entry(issue)
            if len(issues) > 10:
                self._add_log_entry(f"  ... 共 {len(issues)} 个问题")
        else:
            self._add_log_entry(f"✓ 字幕质量检查通过: {path.name}")

    def _open_output_dir(self):
        """打开输出目录——优先使用 worker 回传的精确路径"""
        target = None
        if self._output_paths:
            target = Path(self._output_paths[-1]).parent
        elif self._last_output_dir and self._last_output_dir.exists():
            target = self._last_output_dir
        if not target or not target.exists():
            for jobs in (self.video_jobs, self.subtitle_jobs):
                for job in jobs:
                    candidates = [job.parent / job.stem, job.parent]
                    for c in candidates:
                        if c.exists():
                            target = c
                            break
                    if target:
                        break
                if target:
                    break
        if not target:
            target = Path(self.work_dir)
        try:
            os.startfile(str(target))
            self._add_log_entry(f"已打开目录：{target}")
        except Exception as e:
            self._add_log_entry(f"打开目录失败：{e}")

    def _notify(self, title: str, msg: str):
        """安全的系统通知，避免命令注入"""
        safe_title = re.sub(r"[^\w\s\-_.()\[\]【】]", "", title)[:64]
        safe_msg = re.sub(r"[^\w\s\-_.()\[\]【】，。！？、：；]", "", msg)[:200]

        try:
            from winotify import Notification, audio
            toast = Notification(
                app_id="字幕工具",
                title=safe_title,
                msg=safe_msg,
                duration="short"
            )
            toast.set_audio(audio.Default, loop=False)
            toast.show()
            return
        except ImportError:
            pass
        except Exception as e:
            logger.debug("winotify 通知失败: %s，尝试 PowerShell 备选方案", e)

        try:
            ps_script = (
                "Add-Type -AssemblyName System.Windows.Forms;"
                "$n=New-Object System.Windows.Forms.NotifyIcon;"
                "$n.Icon=[System.Drawing.SystemIcons]::Information;"
                '$n.BalloonTipIcon="Info";'
                f'$n.BalloonTipTitle="{safe_title}";'
                f'$n.BalloonTipText="{safe_msg}";'
                "$n.Visible=$true;"
                f"$n.ShowBalloonTip({cfg.app.notification_duration_ms});"
                f"Start-Sleep -Seconds {cfg.app.notification_sleep_s};"
                "$n.Dispose()"
            )
            import base64
            encoded = base64.b64encode(ps_script.encode("utf-16-le")).decode("ascii")
            subprocess.Popen(
                ["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW
            )
        except Exception as e:
            logger.debug("通知失败: %s", e)

    # ─── 主题 ───

    def _toggle_theme(self):
        self.dark_mode = not self.dark_mode
        self.colors = DARK if self.dark_mode else LIGHT
        self._apply_style()
        self.theme_btn.setText("☀" if not self.dark_mode else "🌙")
        self._add_log_entry(f"已切换至{'深色' if self.dark_mode else '浅色'}模式")

    def _detect_system_dark(self) -> bool:
        try:
            import winreg
            k = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                               r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize")
            v, _ = winreg.QueryValueEx(k, "AppsUseLightTheme")
            winreg.CloseKey(k)
            return v == 0
        except Exception:
            return False

    def closeEvent(self, event):
        self._save_window_state()
        super().closeEvent(event)

    def showEvent(self, event):
        super().showEvent(event)
        # 窗口首次显示后视口宽度已确定，重新计算日志条目高度，避免首条与后续不一致
        self._relayout_log_items()

    def eventFilter(self, obj, event):
        if obj is self.log_list and event.type() == QEvent.Resize:
            self._relayout_log_items()
        return super().eventFilter(obj, event)

    def _relayout_log_items(self):
        """按当前视口宽度重算所有日志条目的 sizeHint，保证高度一致"""
        if getattr(self, "_relayouting", False):
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


def main():
    import sys
    import logging

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    app = QApplication(sys.argv)
    app.setApplicationName("本地字幕生成工具")

    bj_tz = timezone(timedelta(hours=8))
    hour = datetime.now(bj_tz).hour
    peak_periods = "9:00-12:00、14:00-18:00"
    in_peak = (9 <= hour < 12) or (14 <= hour < 18)
    if in_peak:
        reply = QMessageBox.question(
            None, "高峰时段提醒",
            f"当前为 DeepSeek API 高峰时段（{peak_periods}），价格较高。\n是否继续？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply == QMessageBox.No:
            sys.exit(0)

    window = SubtitleApp()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
