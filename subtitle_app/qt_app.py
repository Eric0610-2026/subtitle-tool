#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PySide6/Qt 版主应用窗口
"""
import logging
import os, re, time, traceback
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Dict

logger = logging.getLogger(__name__)

from PySide6.QtCore import Qt, QTimer, QEvent, QSize
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLineEdit, QCheckBox, QPushButton, QListWidgetItem,
    QLabel, QTabWidget, QSplitter,
    QFrame, QFileDialog, QMessageBox,
    QMenu, QDialog, QGridLayout,
)
from PySide6.QtGui import QColor, QFontMetrics

from .srt_utils import (
    SUB_EXTS, fmt_job_display, fmt_duration,
    load_json, save_json, estimate_eta,
    seconds_to_srt_time, srt_time_to_seconds,
    OverallProgress, find_tool, IGNORE_FILE,
    analyze_subtitle_file, format_quality_report,
)
from .config import cfg
from .dialogs import SettingsDialog, show_history_dialog, show_cache_dialog, EmbedDialog, show_embed_confirm_dialog, ExtractDialog
from .muxer import embed_subtitles_to_video, extract_embedded_subtitle, convert_to_mp4
from .widgets import DropListWidget, SCAN_VIDEO_EXTS, AUDIO_EXTS
from .panels import ProgressPanel, PreviewPanel, LogPanel, SignalBridge, _silent_text_input, _silent_double_input
from .theme import load_theme_colors, make_sun_icon, make_moon_icon, detect_system_dark, build_qss
from .notifier import notify as system_notify

APP_DIR = Path(__file__).resolve().parent.parent

# ─── 配色（从 config.json 读取，见 theme.py）───
LIGHT, DARK = load_theme_colors()


def _batch_size_save_field(values: dict) -> Optional[tuple]:
    """决定「永久保存」批大小时应写入 config 的字段。

    翻译端读取规则（见 translator.py）：本地模式读 `translation.batch_size`，
    联网模式读 `translation.batch_size_online`。若把自定义值一律写进
    `batch_size`，联网模式的自定义值重启后会失效，且会污染本地模式默认值。
    返回 (字段名, 值)；自定义值为 None（等于当前模式默认）时返回 None，
    表示不覆盖 config 中原值。
    """
    bs = values.get("translation_batch_size")
    if bs is None:
        return None
    mode = str(values.get("translation_mode", "local")).lower()
    field = "batch_size" if mode == "local" else "batch_size_online"
    return field, bs


class SubtitleApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("🎬 本地字幕生成工具")
        self.resize(cfg.app.window_width, cfg.app.window_height)
        self.setMinimumSize(cfg.app.window_min_width, cfg.app.window_min_height)

        self.dark_mode = detect_system_dark()
        self.colors = DARK if self.dark_mode else LIGHT

        self.work_dir = str(APP_DIR)
        self.video_jobs: List[Path] = []
        self.subtitle_jobs: List[Path] = []
        from .pipeline import SubtitleWorker
        self.worker = SubtitleWorker()
        self._ignore_path = APP_DIR / IGNORE_FILE
        self._migrate_old_progress()
        self._ignore_set = self._load_ignore_set()
        self._start_time: Optional[float] = None
        self._closing = False  # 窗口关闭中：后台线程停止发 UI 信号
        self._manual_embedding = False  # 后台手动嵌入进行中
        self._last_output_dir: Optional[Path] = None  # 记录最后输出目录
        self._output_paths: List[str] = []  # 本轮所有输出文件路径
        self._stats: Dict[str, any] = {}  # 处理统计
        self._quality_reports: List[dict] = []  # 本轮字幕质量报告
        self._overall = None  # 跨文件总进度跟踪
        self._settings_path = Path.home() / ".subtitle_tool_settings.json"

        # ── 信号桥（替代 queue.Queue + QTimer 轮询）──
        self.signal_bridge = SignalBridge()
        self.signal_bridge.event_received.connect(self._handle_event)
        # 事件分发表只构建一次，避免每次事件到达时重建
        self._event_handlers = self._build_event_handlers()
        # ── 构建多方案配置（含向后兼容）──
        _presets_raw = getattr(cfg.translation, "presets", None)
        if _presets_raw:
            _presets = [
                {"id": p.id, "name": p.name,
                 "api_url": p.api_url, "api_key": p.api_key, "model": p.model}
                for p in _presets_raw]
        else:
            _presets = [{"id": "default", "name": "默认方案",
                         "api_url": cfg.translation.api_url,
                         "api_key": cfg.translation.api_key,
                         "model": cfg.translation.model}]
        _active_preset_id = getattr(cfg.translation, "active_preset", _presets[0]["id"])
        _active_preset = next((p for p in _presets if p["id"] == _active_preset_id), _presets[0])
        # 默认配置（来自 config.json）
        self.settings_data = {
            "model_dir": str(APP_DIR / cfg.whisper.model_dir) if (APP_DIR / cfg.whisper.model_dir).exists() else cfg.whisper.model_dir,
            "language": cfg.whisper.language,
            "device": cfg.whisper.device,
            "compute_type": cfg.whisper.compute_type,
            "extract_audio": cfg.whisper.extract_audio,
            "vad_filter": cfg.whisper.vad_filter,
            "default_video_dir": getattr(cfg.app, "default_video_dir", ""),
            "reuse_auto_lang": getattr(cfg.whisper, "reuse_auto_lang", True),
            "target_lang": cfg.translation.target_lang,
            "translation_mode": getattr(cfg.translation, "mode", "local"),
            "translation_model": _active_preset["model"],
            "api_url": _active_preset["api_url"],
            "api_key": _active_preset["api_key"],
            "pipeline": cfg.translation.pipeline,
            "translation_only": False,
            "translation_batch_size": None,  # None=按模式默认（本地 cfg.batch_size / 联网 cfg.batch_size_online）
            "send_all": False,
            "pause_before_embed": getattr(cfg.translation, "pause_before_embed", False),
            "backup_max_files": getattr(cfg.translation, "backup_max_files", 50),
            "presets": _presets,
            "active_preset": _active_preset_id,
        }
        self._build_ui()
        self._apply_style()
        self._model_status_refreshed = False
        self._update_model_status()  # 初始化「当前模型」标签

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
        except (ValueError, OSError, TypeError) as e:
            logger.debug("恢复窗口状态失败: %s", e)

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
        title = QLabel("本地字幕生成工具")
        title.setStyleSheet("color:white; font-size:15px; font-weight:700;")
        hl.addWidget(title)
        hl.addStretch()
        ver = QLabel("Whisper + AI 翻译")
        ver.setStyleSheet("color:#94a3b8; font-size:11px;")
        hl.addWidget(ver)
        hl.addSpacing(8)
        self.theme_btn = QPushButton()
        self.theme_btn.setFixedSize(32, 28)
        self.theme_btn.setIcon(make_moon_icon() if self.dark_mode else make_sun_icon())
        self.theme_btn.setIconSize(QSize(18, 18))
        self.theme_btn.setToolTip("切换浅色/深色主题")
        self.theme_btn.clicked.connect(self._toggle_theme)
        hl.addWidget(self.theme_btn)
        main.addWidget(header)

    def _build_file_list(self, bl):
        """构建文件列表区（splitter + 操作按钮）"""
        left = QFrame()
        left.setObjectName("filePanel")
        ll = QVBoxLayout(left)
        ll.setContentsMargins(8, 6, 8, 0)
        self.tabs = QTabWidget()

        def _make_list(is_video: bool):
            w = DropListWidget(is_video_tab=is_video)
            w.itemClicked.connect(lambda item: self._load_preview(item, is_video))
            w.dropped.connect(lambda paths, is_v: self._add_paths(paths, is_v, check_done=False))
            w.reordered.connect(lambda: self._sync_jobs_from_list(is_video))
            w.setContextMenuPolicy(Qt.CustomContextMenu)
            w.customContextMenuRequested.connect(
                lambda pos: self._show_file_context_menu(w, pos))
            return w

        self.video_list = _make_list(True)
        self.sub_list = _make_list(False)
        self.tabs.addTab(self.video_list, "视频/音频生成字幕")
        self.tabs.addTab(self.sub_list, "已有字幕翻译")
        self.tabs.currentChanged.connect(self._on_tab_changed)
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
        top_splitter = QSplitter(Qt.Horizontal)
        top_splitter.setChildrenCollapsible(False)
        top_splitter.setFixedHeight(340)
        top_splitter.setHandleWidth(6)
        left.setMinimumWidth(0)
        top_splitter.addWidget(left)

        # ── 右侧预览面板 ──
        self.preview_panel = PreviewPanel()
        self.preview_panel.setObjectName("previewPanel")
        self.preview_panel.connect_toolbar(self._find_in_preview, self._save_preview, self._offset_preview_time)
        self.preview_panel.fileDropped.connect(self._on_preview_file_dropped)
        top_splitter.addWidget(self.preview_panel)
        top_splitter.setSizes([540, 660])
        top_splitter.setStretchFactor(0, 1)
        top_splitter.setStretchFactor(1, 1)
        return top_splitter

    def _build_progress_and_log(self, bl):
        """构建进度 + 日志面板"""
        self.progress_panel = ProgressPanel()
        self.progress_panel.setObjectName("progressPanel")
        self.log_panel = LogPanel()
        self.log_panel.setObjectName("logPanel")
        self.log_panel.log_list.installEventFilter(self)
        return self.progress_panel, self.log_panel

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        main = QVBoxLayout(central)
        main.setContentsMargins(0, 0, 0, 0)
        main.setSpacing(0)
        self._build_header(main)

        # ── 主体内容 ──
        body = QWidget()
        body.setContentsMargins(16, 10, 16, 8)
        bl = QVBoxLayout(body)
        bl.setSpacing(8)

        # ── 路径行 + 翻译开关 + 更多设置 ──
        pr = QHBoxLayout()
        pr.setSpacing(6)
        pr.addWidget(QLabel("视频目录"))
        self.video_dir = QLineEdit()
        self.video_dir.setPlaceholderText("选择视频目录...")
        pr.addWidget(self.video_dir, 1)
        pr.addWidget(self._make_btn("📂 浏览", self._choose_video_dir))
        pr.addWidget(self._make_btn("📌 默认", self._set_default_video_dir))
        pr.addSpacing(12)
        self.trans_cb = QCheckBox("🌍 开启 AI 翻译")
        self.trans_cb.setChecked(True)
        pr.addWidget(self.trans_cb)
        pr.addWidget(self._make_btn("⚙ 更多设置", self._open_settings, object_name="accentBtn"))
        bl.addLayout(pr)

        # ── 田字型主体：左上文件、右上预览、左下进度、右下日志 ──
        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(6)
        grid.setVerticalSpacing(6)
        top_splitter = self._build_file_list(bl)
        progress_panel, log_panel = self._build_progress_and_log(bl)
        grid.addWidget(top_splitter.widget(0), 0, 0)
        grid.addWidget(top_splitter.widget(1), 0, 1)
        grid.addWidget(progress_panel, 1, 0)
        grid.addWidget(log_panel, 1, 1)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        grid.setRowStretch(0, 3)
        grid.setRowStretch(1, 2)
        bl.addLayout(grid, 1)

        main.addWidget(body, 1)

        # ── 操作按钮 ──
        ar = QHBoxLayout()
        ar.setContentsMargins(16, 6, 16, 10)
        ar.setSpacing(8)
        self.start_btn = self._make_btn("▶ 开始处理", self._start, object_name="startBtn")
        ar.addWidget(self.start_btn)
        self.stop_btn = self._make_btn("⏹ 停止", self._stop, object_name="stopBtn")
        self.stop_btn.setEnabled(False)
        ar.addWidget(self.stop_btn)
        # ── 当前模型状态（信息展示：Whisper/本地/联网大模型具体到种类） ──
        self.model_status = QLabel("🧠 当前模型：…")
        self.model_status.setStyleSheet(f"color:{self.colors['text_muted']}; font-size:11px; padding:0 4px;")
        ar.addWidget(self.model_status)
        ar.addWidget(self._make_btn("🔄 重试", self._retry, object_name="bottomBtn",
                           stylesheet=f"QPushButton {{ background:{self.colors['accent']}; color:white; border:none; }} "
                                      "QPushButton:hover { background:#4f46e5; }"))
        ar.addWidget(self._make_btn("📋 历史", self._show_history, object_name="bottomBtn"))
        ar.addWidget(self._make_btn("💾 缓存", self._show_cache, object_name="bottomBtn"))
        ar.addWidget(self._make_btn("📦 嵌入字幕", self._manual_embed, object_name="bottomBtn"))
        ar.addWidget(self._make_btn("📤 提取字幕", self._manual_extract, object_name="bottomBtn"))
        ar.addWidget(self._make_btn("📤 导出", self._export_log, object_name="bottomBtn"))
        ar.addStretch()
        main.addLayout(ar)

    # ─── 样式 ───

    def _apply_style(self):
        self.setStyleSheet(build_qss(self.colors, self.dark_mode))

    # ─── 交互 ───

    def _choose_video_dir(self):
        """浏览并选择视频目录（类似 missav-downloader 的「保存到」风格）"""
        path = QFileDialog.getExistingDirectory(self, "选择视频目录", self.video_dir.text())
        if path:
            self.video_dir.setText(path)
            self._scan_path(path, True)

    def _default_dir(self) -> str:
        """当前默认视频目录：优先本次会话设置，其次 config.json"""
        return str((self.settings_data or {}).get("default_video_dir") or
                   getattr(cfg.app, "default_video_dir", "") or "")

    def _set_default_video_dir(self):
        d = self._default_dir()
        if not d:
            self._add_log_entry("未配置默认视频目录（app.default_video_dir），可在「⚙ 更多设置」中配置")
            return
        if not Path(d).exists():
            self._add_log_entry(f"默认视频目录不存在：{d}，跳过扫描")
            return
        self.video_dir.setText(d)
        # 按当前标签页类型扫描：视频页扫视频/音频，字幕页扫字幕
        is_video = self.tabs.currentIndex() == 0
        self._scan_path(d, is_video)
        kind = "视频/音频" if is_video else "字幕"
        self._add_log_entry(f"已从默认目录扫描{kind}文件：{d}")

    def _open_settings(self):
        dlg = SettingsDialog(self, self.settings_data)
        result = dlg.exec()
        if result == 1:
            self.settings_data = dlg.get_values()
            self._add_log_entry("设置已应用（本次运行有效）")
            self._update_model_status()  # 翻译模型种类可能已变化，刷新标签
        elif result == 2:
            self.settings_data = dlg.get_values()
            self._save_settings_permanently(dlg.get_values())
            self._add_log_entry("设置已保存到 config.json（永久生效）")
            self._update_model_status()

    def _save_settings_permanently(self, values: dict):
        import json
        path = Path(__file__).resolve().parent / "config.json"
        if not path.exists():
            QMessageBox.warning(self, "保存失败", "未找到 config.json，请先复制 config.example.json")
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            QMessageBox.warning(self, "保存失败",
                                f"config.json 读取失败（可能被手动编辑损坏）：\n{e}")
            return
        raw.setdefault("app", {})["default_video_dir"] = values.get("default_video_dir", "")
        raw.setdefault("whisper", {})["model_dir"] = values.get("model_dir", "")
        raw["whisper"]["language"] = values.get("language", "auto")
        raw["whisper"]["device"] = values.get("device", "cuda")
        raw["whisper"]["compute_type"] = values.get("compute_type", "int8_float16")
        raw["whisper"]["extract_audio"] = values.get("extract_audio", True)
        raw["whisper"]["vad_filter"] = values.get("vad_filter", True)
        raw["whisper"]["reuse_auto_lang"] = values.get("reuse_auto_lang", True)
        trans = raw.setdefault("translation", {})
        trans["target_lang"] = values.get("target_lang", "zh")
        trans["mode"] = values.get("translation_mode", "local")
        trans["model"] = values.get("translation_model", "")
        trans["api_url"] = values.get("api_url", "")
        trans["api_key"] = values.get("api_key", "")
        trans["pipeline"] = values.get("pipeline", True)
        _bs_save = _batch_size_save_field(values)
        if _bs_save is not None:  # 自定义值才写入 config.json；默认值不覆盖
            field, _bs_val = _bs_save
            trans[field] = _bs_val
        trans["pause_before_embed"] = values.get("pause_before_embed", False)
        trans["backup_max_files"] = values.get("backup_max_files", 50)
        # 保存多方案配置
        presets = values.get("presets")
        if presets:
            trans["presets"] = presets
        trans["active_preset"] = values.get("active_preset", "default")
        try:
            path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as e:
            QMessageBox.warning(self, "保存失败", f"写入 config.json 失败：{e}")
            return
        cfg.reload()


    def _add_files(self, is_video: bool):
        exts = SCAN_VIDEO_EXTS | AUDIO_EXTS if is_video else SUB_EXTS
        ext_str = " ".join(f"*{e}" for e in sorted(exts))
        files, _ = QFileDialog.getOpenFileNames(
            self, "选择文件", self.video_dir.text(),
            f"媒体文件 ({ext_str})")
        if files:
            self._add_paths([Path(f) for f in files], is_video, check_done=False)

    def _add_folder(self, is_video: bool):
        d = QFileDialog.getExistingDirectory(self, "选择文件夹", self.video_dir.text())
        if d:
            exts = SCAN_VIDEO_EXTS | AUDIO_EXTS if is_video else SUB_EXTS
            paths = []
            for f in sorted(Path(d).iterdir()):
                if f.suffix.lower() in exts:
                    paths.append(f)
            self._add_paths(paths, is_video, check_done=False)
            self._add_log_entry(f"已扫描文件夹：{d}")

    def _load_done_set(self):
        done = set()
        done_stems = set()
        data = load_json(self._ignore_path, {})
        for path_str in data.get("done", []):
            done.add(path_str)
            done_stems.add(Path(path_str).stem)
            first_part = Path(path_str).stem.split(".")[0]
            if first_part != Path(path_str).stem:
                done_stems.add(first_part)
        if done:
            self._add_log_entry(f"历史记录：{len(done)} 个已完成文件")
        return done, done_stems

    def _migrate_old_progress(self):
        old = APP_DIR / ".subtitle_progress.json"
        if not old.exists():
            return
        if self._ignore_path.exists():
            old.unlink()
            return
        data = load_json(old, {})
        data.setdefault("ignored", [])
        save_json(self._ignore_path, data)
        old.unlink()
        self._add_log_entry("已迁移历史记录到新版忽略文件")

    def _load_ignore_set(self):
        data = load_json(self._ignore_path, {})
        ignored = set(data.get("ignored", []))
        if ignored:
            self._add_log_entry(f"已加载 {len(ignored)} 个忽略文件")
        return ignored

    def _save_ignore(self):
        data = load_json(self._ignore_path, {})
        data["ignored"] = sorted(self._ignore_set)
        save_json(self._ignore_path, data)

    def _is_ignored(self, path: Path) -> bool:
        return str(path.resolve()) in self._ignore_set

    def _toggle_ignore(self, item):
        lb = self.video_list if self.tabs.currentIndex() == 0 else self.sub_list
        jobs = self.video_jobs if self.tabs.currentIndex() == 0 else self.subtitle_jobs
        row = lb.row(item)
        if row < 0 or row >= len(jobs):
            return
        path = jobs[row]
        resolved = str(path.resolve())
        if resolved in self._ignore_set:
            self._ignore_set.discard(resolved)
            self._add_log_entry(f"已取消忽略：{path.name}")
        else:
            self._ignore_set.add(resolved)
            self._add_log_entry(f"已忽略：{path.name}")
        self._save_ignore()
        self._refresh_item_visual(item)

    def _refresh_item_visual(self, item):
        path_str = item.data(Qt.UserRole)
        font = item.font()
        if path_str and str(Path(path_str).resolve()) in self._ignore_set:
            font.setStrikeOut(True)
            item.setForeground(QColor("#94a3b8"))
            item.setFont(font)
        else:
            font.setStrikeOut(False)
            item.setForeground(QColor())
            item.setFont(font)

    def _add_paths(self, paths: List[Path], is_video: bool, check_done: bool = True):
        lb = self.video_list if is_video else self.sub_list
        jobs = self.video_jobs if is_video else self.subtitle_jobs
        exts = SCAN_VIDEO_EXTS | AUDIO_EXTS if is_video else SUB_EXTS
        existing = {str(p.resolve()) for p in jobs}
        done, done_stems = self._load_done_set() if check_done else (set(), set())
        added = 0
        skipped = 0
        for p in paths:
            if p.suffix.lower() not in exts:
                continue
            resolved = str(p.resolve())
            if resolved in existing:
                continue
            if check_done and (resolved in done or p.stem in done_stems or str(p) in done):
                skipped += 1
                continue
            jobs.append(p)
            existing.add(resolved)
            item = QListWidgetItem(fmt_job_display(p))
            item.setData(Qt.UserRole, str(p))
            lb.addItem(item)
            if self._is_ignored(p):
                self._refresh_item_visual(item)
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

    def _on_tab_changed(self, index: int):
        """切换标签页时不做任何自动扫描（扫描由「📌 默认」按钮手动触发）"""
        pass

    def _scan_dir(self):
        is_video = self.tabs.currentIndex() == 0
        path = self.video_dir.text()
        if path:
            self._scan_path(path, is_video)

    def _remove_selected(self):
        is_video = self.tabs.currentIndex() == 0
        lb = self.video_list if is_video else self.sub_list
        jobs = self.video_jobs if is_video else self.subtitle_jobs
        removed = set()
        for item in reversed(sorted(lb.selectedItems(), key=lambda x: lb.row(x))):
            row = lb.row(item)
            if 0 <= row < len(jobs):
                jobs.pop(row)
            lb.takeItem(row)
            removed.add(row)
        self._add_log_entry(f"已移除 {len(removed)} 个选中项")

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

    def _show_file_context_menu(self, lb, pos):
        item = lb.itemAt(pos)
        if not item:
            return
        menu = QMenu()
        path_str = item.data(Qt.UserRole)
        if path_str and str(Path(path_str).resolve()) in self._ignore_set:
            action = QAction("取消忽略", self)
        else:
            action = QAction("忽略此文件", self)
        action.triggered.connect(lambda: self._toggle_ignore(item))
        menu.addAction(action)
        menu.exec(lb.viewport().mapToGlobal(pos))

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
                self.preview_panel.set_text(c.read_text(encoding="utf-8"))
                self.preview_panel.last_output_dir = c.parent
                return
        self.preview_panel.clear()

    def _on_preview_file_dropped(self, path: str):
        """拖入字幕到预览区时，同时加入已有字幕翻译列表"""
        self._add_paths([Path(path)], is_video=False, check_done=False)
        self.tabs.setCurrentIndex(1)
        resolved = str(Path(path).resolve())
        for i in range(self.sub_list.count()):
            if self.sub_list.item(i).data(Qt.UserRole) == resolved:
                self.sub_list.setCurrentRow(i)
                break

    def _find_in_preview(self):
        """在预览区表格中查找原文/译文，并高亮命中行"""
        text, ok = _silent_text_input(self, "查找", "输入要查找的文本：")
        if not ok or not text:
            return
        table = self.preview_panel.preview
        self.preview_panel.clear_highlight()
        hit_rows = []
        for r in range(table.rowCount()):
            for col in (2, 3):  # 原文 / 译文
                item = table.item(r, col)
                if item and text in item.text():
                    hit_rows.append(r)
                    break
        if hit_rows:
            self.preview_panel.highlight_rows(hit_rows)
            self._add_log_entry(f"预览区查找完成：{text}（命中 {len(hit_rows)} 行）")
        else:
            self._add_log_entry(f"预览区未找到：{text}")

    def _offset_preview_time(self):
        """批量调整预览区字幕时间戳"""
        content = self.preview_panel.get_text().strip()
        if not content:
            QMessageBox.information(self, "提示", "预览区为空")
            return
        offset, ok = _silent_double_input(self, "时间偏移",
                                           "偏移量（秒）：正数=延后，负数=提前")
        if not ok:
            return
        # 匹配所有 SRT 时间戳行：HH:MM:SS,mmm --> HH:MM:SS,mmm
        ts_re = re.compile(r"(\d+:\d{1,2}:\d{1,2}[,.]\d{1,3})\s*-->\s*(\d+:\d{1,2}:\d{1,2}[,.]\d{1,3})")

        def _shift(m):
            start = max(0, srt_time_to_seconds(m.group(1)) + offset)
            end = max(0, srt_time_to_seconds(m.group(2)) + offset)
            return f"{seconds_to_srt_time(start)} --> {seconds_to_srt_time(end)}"

        new_content = ts_re.sub(_shift, content)
        self.preview_panel.set_text(new_content)
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
            srt_path.write_text(self.preview_panel.get_text(), encoding="utf-8")
            self._add_log_entry(f"已保存预览修改：{srt_path.name}")
        except OSError as e:
            QMessageBox.warning(self, "保存失败", str(e))

    # ─── 处理控制 ───

    def _build_opts(self, skip_completed=False):
        s = self.settings_data
        # 翻译方式：local=本地 Hy-MT2（强制走本机 8080 服务，无需配置）；online=使用用户预设的联网 API
        mode = str(s.get("translation_mode", "local")).lower()
        if mode == "local":
            api_url, api_key, tmodel = (
                "http://127.0.0.1:8080/v1/chat/completions", "local", "hy-mt2")
        else:
            api_url, api_key, tmodel = (
                s.get("api_url", ""), s.get("api_key", ""), s.get("translation_model", ""))
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
            "reuse_auto_lang": s.get("reuse_auto_lang", True),
            "api_url": api_url,
            "api_key": api_key,
            "translation_model": tmodel,
            "translation_only": s.get("translation_only", False),
            "translation_batch_size": s.get("translation_batch_size"),
            "send_all": s.get("send_all", False),
            "pause_before_embed": s.get("pause_before_embed", False),
            "skip_completed": skip_completed,
            "concurrency": cfg.translation.concurrency_pipeline if s.get("pipeline", True) else cfg.translation.concurrency_serial,
            "post": self.signal_bridge.post,
            "_is_stopped": lambda: self.worker.stop_requested,
            "_register_proc": self.worker._register_proc,
            "_unregister_proc": self.worker._unregister_proc,
        }

    def _begin_processing(self, jobs, opts, log_msg):
        self._start_time = time.time()
        self._stats = {"files": len(jobs)}
        self._quality_reports = []
        self._output_paths = []
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self._reset_progress()
        self.preview_panel.clear()
        self._add_log_entry(log_msg)
        w = getattr(cfg.progress, "transcribe_weight", 80.0) if hasattr(cfg, "progress") else 80.0
        self._overall = OverallProgress(len(jobs), transcribe_weight=w)
        self._overall.start()
        self.progress_panel.overall_progress.setValue(0)
        self.progress_panel.overall_label.setText(f"总进度：第 1/{len(jobs)} 个 · 已完成 0% · 等待中")
        self.worker.start(jobs, opts)

    def _get_jobs(self):
        is_video = self.tabs.currentIndex() == 0
        return self.video_jobs if is_video else self.subtitle_jobs

    def _active_jobs(self):
        """返回所有未被忽略的作业"""
        return [j for j in self._get_jobs() if not self._is_ignored(j)]

    def _start(self):
        if self.worker.thread and self.worker.thread.is_alive():
            QMessageBox.warning(self, "提示", "正在处理中")
            return
        if getattr(self, "_manual_embedding", False) or getattr(self, "_manual_extracting", False):
            QMessageBox.warning(self, "提示", "后台嵌入/提取任务正在执行中，请等待完成")
            return
        jobs = self._active_jobs()
        total = len(self._get_jobs())
        skipped = total - len(jobs)
        if not jobs:
            QMessageBox.warning(self, "提示", "队列为空" + ("（所有文件已被忽略）" if skipped else ""))
            return
        if not self._confirm_peak_hours():
            return
        msg = f"开始处理，队列 {len(jobs)} 个文件"
        if skipped:
            msg += f"（已跳过 {skipped} 个忽略文件）"
        self._begin_processing(jobs, self._build_opts(False), msg)

    def _set_elided(self, label: QLabel, text: str) -> None:
        fm = QFontMetrics(label.font())
        w = max(label.width(), 200)
        label.setText(fm.elidedText(text, Qt.ElideRight, w))

    def _reset_progress(self):
        self.progress_panel.reset()
        self._model_status_refreshed = False

    def _stop(self):
        if not (self.worker.thread and self.worker.thread.is_alive()):
            return
        if not self._confirm("停止确认", "确定要停止当前处理吗？\n已完成处理的文件不会丢失。", default_no=True):
            return
        self.worker.stop()
        self._add_log_entry("已请求停止")

    def _update_model_status(self):
        """更新主页面「当前加载模型」标签：只列出实际已加载/已生效的模型，未加载的不显示"""
        from .local_service import is_service_running
        s = self.settings_data
        parts = []

        # Whisper：仅当已加载才显示
        if self.worker.transcriber.is_loaded():
            mdir = Path(s.get("model_dir", ""))
            ver = mdir.name if mdir.name and mdir.name not in (".", "/", "\\") else "Whisper"
            parts.append(f"Whisper {ver}")

        # 翻译模型：本地仅运行中显示；联网始终显示（当前生效的翻译模型，无本地加载态）
        mode = str(s.get("translation_mode", "local")).lower()
        tmodel = (s.get("translation_model") or "").strip()
        local_running = False
        if mode == "local":
            local_running = is_service_running()
            if local_running:
                parts.append(f"本地模型 {tmodel or 'Hy-MT2'}")
        elif mode == "online" and s.get("api_url"):
            parts.append(f"联网模型 {tmodel or '未配置'}")

        if parts:
            self.model_status.setText("🧠 当前加载：" + " · ".join(parts))
            self.model_status.setStyleSheet("color:#22c55e; font-size:11px; padding:0 4px;")
        else:
            self.model_status.setText("🧠 当前加载：—")
            self.model_status.setStyleSheet(f"color:{self.colors['text_muted']}; font-size:11px; padding:0 4px;")

    def _retry(self):
        if self.worker.thread and self.worker.thread.is_alive():
            QMessageBox.warning(self, "提示", "正在处理中")
            return
        jobs = self._active_jobs()
        total = len(self._get_jobs())
        skipped = total - len(jobs)
        if not jobs:
            QMessageBox.warning(self, "提示", "队列为空" + ("（所有文件已被忽略）" if skipped else ""))
            return
        if not self._confirm_peak_hours():
            return
        msg = f"断点续翻，检查 {len(jobs)} 个文件..."
        if skipped:
            msg += f"（已跳过 {skipped} 个忽略文件）"
        self._begin_processing(jobs, self._build_opts(True), msg)

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
        """打开嵌入字幕对话框，支持批量选择视频+字幕嵌入为 MKV。

        嵌入在后台线程执行（daemon），UI 保持响应；完成后通过
        manual_embed_done 事件弹窗汇总结果。
        """
        if getattr(self, "_manual_embedding", False):
            QMessageBox.warning(self, "提示", "嵌入任务正在执行中，请等待完成")
            return
        if self.worker.thread and self.worker.thread.is_alive():
            QMessageBox.warning(self, "提示", "主任务正在处理中，请先停止再嵌入")
            return
        ffmpeg = find_tool("ffmpeg.exe", APP_DIR) or find_tool("ffmpeg", APP_DIR)
        if not ffmpeg:
            QMessageBox.warning(self, "错误", "未找到 ffmpeg，请放在应用目录下")
            return

        dlg = EmbedDialog(self, self.video_dir.text())
        if dlg.exec() != QDialog.Accepted:
            return

        pairs = dlg.get_pairs()
        if not pairs:
            return
        if not self._confirm("确认嵌入",
                f"确定要嵌入这 {len(pairs)} 个任务？\n\n"
                "任务将在后台执行，界面可正常操作，完成后会弹出结果提示。"):
            return

        self._manual_embedding = True
        self._add_log_entry(f"📦 开始后台嵌入 {len(pairs)} 个任务...")
        import threading
        threading.Thread(target=self._manual_embed_worker,
                         args=(pairs, ffmpeg), daemon=True).start()

    def _manual_embed_worker(self, pairs: list, ffmpeg: str) -> None:
        """后台嵌入线程：逐个执行 ffmpeg 内嵌，日志经信号桥回传 UI。

        异常路径也要保证发送 manual_embed_done 复位状态，避免
        _manual_embedding 永久锁死手动嵌入功能；窗口关闭（_closing）
        后不再发信号，daemon 线程随进程退出。
        """
        total = len(pairs)
        success = 0

        def post(msg):
            if msg.get("type") == "log":
                self.signal_bridge.post({
                    "type": "log",
                    "message": msg.get("message", ""),
                    "level": msg.get("level", "INFO"),
                })

        try:
            for i, (video, srt) in enumerate(pairs, 1):
                if getattr(self, "_closing", False):
                    break
                self.signal_bridge.post({
                    "type": "log",
                    "message": f"📦 [{i}/{total}] 嵌入: {video.name} + {srt.name}",
                })
                mkv, _ = embed_subtitles_to_video(video, srt, ffmpeg, post)
                if mkv and mkv.exists():
                    success += 1
                    self.signal_bridge.post({
                        "type": "log", "message": f"✅ [{i}/{total}] 嵌入完成: {mkv.name}",
                    })
                    if str(mkv.resolve()) not in self._output_paths:
                        self._output_paths.append(str(mkv.resolve()))
                    try:
                        video.unlink()
                        srt.unlink()
                        self.signal_bridge.post({
                            "type": "log", "message": f"已删除原文件: {video.name}, {srt.name}",
                        })
                    except OSError as e:
                        self.signal_bridge.post({
                            "type": "log", "message": f"删除原文件失败: {e}", "level": "WARNING",
                        })
                else:
                    self.signal_bridge.post({
                        "type": "log", "message": f"❌ [{i}/{total}] 嵌入失败: {video.name}",
                        "level": "WARNING",
                    })
        except Exception as e:
            logger.error("后台嵌入线程异常: %s\n%s", e, traceback.format_exc())
            try:
                self.signal_bridge.post({
                    "type": "log", "message": f"后台嵌入线程异常: {e}", "level": "ERROR",
                })
            except Exception:
                pass
        finally:
            # 无论成功/异常/关闭，都复位标志；窗口已关闭时 UI 侧跳过弹窗
            try:
                self.signal_bridge.post({
                    "type": "manual_embed_done", "success": success, "total": total,
                })
            except Exception:
                pass

    def _on_manual_embed_done(self, e):
        """后台嵌入结束：恢复状态并弹出结果汇总（窗口关闭中则跳过弹窗）"""
        self._manual_embedding = False
        if getattr(self, "_closing", False):
            return
        success = e.get("success", 0)
        total = e.get("total", 0)
        if success:
            QMessageBox.information(self, "嵌入完成", f"成功嵌入 {success}/{total} 个文件")
        else:
            QMessageBox.warning(self, "嵌入失败", "所有文件嵌入失败，请查看日志")

    # ─── 手动提取内嵌字幕 ───

    def _manual_extract(self):
        """打开提取字幕对话框，批量提取 MKV 内嵌的第一个字幕流为 SRT。

        提取在后台线程执行（daemon），UI 保持响应；完成后通过
        manual_extract_done 事件弹窗汇总结果。
        """
        if getattr(self, "_manual_extracting", False):
            QMessageBox.warning(self, "提示", "提取任务正在执行中，请等待完成")
            return
        if self.worker.thread and self.worker.thread.is_alive():
            QMessageBox.warning(self, "提示", "主任务正在处理中，请先停止再提取")
            return
        ffmpeg = find_tool("ffmpeg.exe", APP_DIR) or find_tool("ffmpeg", APP_DIR)
        if not ffmpeg:
            QMessageBox.warning(self, "错误", "未找到 ffmpeg，请放在应用目录下")
            return

        dlg = ExtractDialog(self, self.video_dir.text())
        if dlg.exec() != QDialog.Accepted:
            return

        files = dlg.get_files()
        if not files:
            return
        convert_mp4 = dlg.should_convert_to_mp4()
        extra = "\n\n勾选了「提取后转为 MP4」：提取完成后将转换格式，并在转换验证通过后删除原文件。" if convert_mp4 else ""
        if not self._confirm("确认提取",
                f"确定要提取这 {len(files)} 个文件的内嵌字幕？{extra}\n\n"
                "任务将在后台执行，界面可正常操作，完成后会弹出结果提示。"):
            return

        self._manual_extracting = True
        self._add_log_entry(f"📤 开始后台提取 {len(files)} 个字幕...")
        import threading
        threading.Thread(target=self._manual_extract_worker,
                         args=(files, ffmpeg, convert_mp4), daemon=True).start()

    def _manual_extract_worker(self, files: list, ffmpeg: str, convert_mp4: bool = False) -> None:
        """后台提取线程：逐个执行 ffmpeg 提取，可选转 MP4，日志经信号桥回传 UI。

        异常路径也要保证发送 manual_extract_done 复位状态，避免
        _manual_extracting 永久锁死提取功能；窗口关闭（_closing）后
        不再发信号，daemon 线程随进程退出。
        """
        total = len(files)
        success = 0

        def post(msg):
            if msg.get("type") == "log":
                self.signal_bridge.post({
                    "type": "log",
                    "message": msg.get("message", ""),
                    "level": msg.get("level", "INFO"),
                })

        try:
            for i, video in enumerate(files, 1):
                if getattr(self, "_closing", False):
                    break
                self.signal_bridge.post({
                    "type": "log",
                    "message": f"📤 [{i}/{total}] 提取: {video.name}",
                })
                srt, status = extract_embedded_subtitle(video, ffmpeg, post)
                if srt and srt.exists():
                    success += 1
                    self.signal_bridge.post({
                        "type": "log", "message": f"✅ [{i}/{total}] {status}",
                    })
                    try:
                        self.signal_bridge.post({
                            "type": "output_path", "path": str(srt),
                        })
                    except Exception:
                        pass
                    if convert_mp4:
                        self._convert_and_cleanup(video, ffmpeg, post, i, total)
                else:
                    self.signal_bridge.post({
                        "type": "log", "message": f"❌ [{i}/{total}] 提取失败: {video.name} — {status}",
                        "level": "WARNING",
                    })
        except Exception as e:
            logger.error("后台提取线程异常: %s\n%s", e, traceback.format_exc())
            try:
                self.signal_bridge.post({
                    "type": "log", "message": f"后台提取线程异常: {e}", "level": "ERROR",
                })
            except Exception:
                pass
        finally:
            # 无论成功/异常/关闭，都复位标志；窗口已关闭时 UI 侧跳过弹窗
            try:
                self.signal_bridge.post({
                    "type": "manual_extract_done", "success": success, "total": total,
                })
            except Exception:
                pass

    def _convert_and_cleanup(self, video, ffmpeg: str, post, i: int, total: int) -> None:
        """提取成功后转 MP4；转换验证通过才删除原 MKV，否则保留并警告。

        删除原文件是对用户不可逆的操作，因此只在 convert_to_mp4 明确
        返回 is_trustworthy=True（时长验证通过）时执行。
        """
        mp4, trustworthy = convert_to_mp4(video, ffmpeg, post)
        if mp4 and mp4.exists():
            self.signal_bridge.post({
                "type": "log", "message": f"✅ [{i}/{total}] 转换完成: {mp4.name}",
            })
            try:
                self.signal_bridge.post({
                    "type": "output_path", "path": str(mp4),
                })
            except Exception:
                pass
            # 防误删：mp4 与源是同一文件（源本身就是 .mp4 跳过转换）时绝不删除
            same_file = Path(mp4).resolve() == Path(video).resolve()
            if trustworthy and not same_file:
                try:
                    video.unlink()
                    self.signal_bridge.post({
                        "type": "log",
                        "message": f"✅ [{i}/{total}] 已删除原文件: {video.name}",
                    })
                except OSError as e:
                    self.signal_bridge.post({
                        "type": "log",
                        "message": f"删除原文件失败: {e}（保留原文件）", "level": "WARNING",
                    })
            elif not trustworthy:
                self.signal_bridge.post({
                    "type": "log",
                    "message": f"⚠️ [{i}/{total}] 时长验证未通过，保留原文件: {video.name}",
                    "level": "WARNING",
                })
            # same_file 时（源已是 MP4）无需删除，静默跳过
        else:
            self.signal_bridge.post({
                "type": "log",
                "message": f"❌ [{i}/{total}] 转换失败，保留原文件: {video.name}",
                "level": "WARNING",
            })

    def _on_manual_extract_done(self, e):
        """后台提取结束：恢复状态并弹出结果汇总（窗口关闭中则跳过弹窗）"""
        self._manual_extracting = False
        if getattr(self, "_closing", False):
            return
        success = e.get("success", 0)
        total = e.get("total", 0)
        if success:
            QMessageBox.information(self, "提取完成", f"成功提取 {success}/{total} 个文件")
        else:
            QMessageBox.warning(self, "提取失败", "所有文件提取失败，请查看日志")

    def _add_log_entry(self, message: str, level: str = "INFO", trace: str = None) -> None:
        # 持久化到日志文件
        py_level = getattr(logging, level.upper(), logging.INFO)
        logger.log(py_level, "%s", message)
        if trace:
            logger.debug("Traceback:\n%s", trace.rstrip())

        # 显示到 UI 日志面板
        self.log_panel.add_entry(message, level, trace)
        self.log_panel.trim_to(cfg.app.max_log_lines)

    def _run_startup_checks(self):
        # ── 检查 config.json 是否存在 ──
        config_path = Path(__file__).resolve().parent / "config.json"
        config_example = config_path.with_name("config.example.json")
        if not config_path.exists() and config_example.exists():
            msg = (
                "首次使用请先创建配置文件，以便永久保存你的设置。\n\n"
                f"将 {config_example.name} 复制并重命名为 {config_path.name}：\n"
                f"  1. 复制 {config_example.name}\n"
                f"  2. 粘贴并重命名为 {config_path.name}\n"
                f"  3. 编辑 {config_path.name}，填入你的 API 地址、密钥和模型名称\n\n"
                "如果没有 config.json，应用会加载默认配置运行，但「永久保存」按钮不可用。\n"
                "（仍可通过「本次有效」按钮在当前会话中使用所有功能。）"
            )
            self._add_log_entry(
                f"未找到 {config_path.name}，已从 {config_example.name} 加载默认配置。"
                f"请复制为 {config_path.name} 并编辑 API 信息", "WARNING")
            QMessageBox.information(self, "首次使用提醒", msg)

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
        model_dir = APP_DIR / cfg.whisper.model_dir if (APP_DIR / cfg.whisper.model_dir).exists() else Path(cfg.whisper.model_dir)
        if not model_dir.is_dir() or not (model_dir / "model.bin").is_file():
            self._add_log_entry(f"未找到 faster-whisper 模型，请下载后放入 {cfg.whisper.model_dir}/ 目录（下载地址：https://www.modelscope.cn/models/pengzhendong/faster-whisper-large-v3-turbo/summary）", "WARNING")
        s = self.settings_data
        if str(s.get("translation_mode", "local")).lower() != "local" and (not s.get("api_url") or not s.get("api_key")):
            self._add_log_entry("API 地址或密钥未设置，请在设置中配置后使用翻译功能", "WARNING")

    def _export_log(self):
        """导出当前日志列表到文件"""
        if self.log_panel.count() == 0:
            QMessageBox.information(self, "导出日志", "日志为空，无需导出。")
            return

        lines = self.log_panel.get_all_lines()

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

    def _handle_progress(self, e):
        p = self.progress_panel
        pct = e.get("percent", 0)
        stage = e.get("stage", "")
        detail = e.get("detail", "")
        # 转写相关 stage：含「转写完成」（旧逻辑只认「转写中」，完成事件 percent=100 被丢弃，
        # 条会停在最后一段 seg_end/duration，并行流水线里翻译已开始时更明显）
        if stage in ("提取音频", "加载模型", "读取字幕", "转写中", "转写完成"):
            bar_pct = 100 if stage == "转写完成" else int(pct)
            p.transcribe_bar.setValue(bar_pct)
            p.transcribe_bar.setFormat(f"{bar_pct}%")
            if detail:
                p.transcribe_detail.setText(detail)
        elif stage == "翻译":
            # 进入翻译说明当前文件转写已结束；若条未满则补到 100%
            if p.transcribe_bar.value() < 100:
                p.transcribe_bar.setValue(100)
                p.transcribe_bar.setFormat("100%")
            p.translate_bar.setValue(int(pct))
            p.translate_bar.setFormat(f"{int(pct)}%")
            p.translate_detail.setText(detail)
            # 翻译阶段已开始：本地模型可能已加载，刷新「当前模型」标签（不再是 whisper）
            if not self._model_status_refreshed:
                self._model_status_refreshed = True
                self._update_model_status()
        elif stage in ("组织输出", "完成", "跳过"):
            p.transcribe_bar.setValue(100)
            p.transcribe_bar.setFormat("100%")
            p.translate_bar.setValue(100)
            p.translate_bar.setFormat("100%")
        # 转写/翻译阶段已设置对应子进度条的详情，跳过底部 detail_label
        if stage not in ("提取音频", "加载模型", "读取字幕", "转写中", "转写完成", "翻译"):
            if detail and self._start_time and pct:
                self._set_detail_with_eta(p, detail, pct)
            elif detail:
                p.detail_label.setText(detail)
            elif self._start_time and pct:
                self._set_detail_with_eta(p, "", pct)
            else:
                p.detail_label.setText("")
        idx = e.get("idx", 0)
        if idx and self._overall is not None:
            overall_pct = self._overall.tick(idx, pct, stage)
            p.overall_progress.setValue(int(overall_pct))
            remain, finish = self._overall.eta()
            p.overall_label.setText(
                f"总进度：第 {idx}/{self._overall.total} 个 · 已完成 {overall_pct:.0f}% · "
                f"预计全部完成 {finish}（剩余 {remain}）")

    def _handle_done(self, e):
        p = self.progress_panel
        msg = e.get("message", "完成")
        self._add_log_entry(msg, "INFO")
        p.transcribe_bar.setValue(100)
        p.transcribe_bar.setFormat("100%")
        p.translate_bar.setValue(100)
        p.translate_bar.setFormat("100%")
        p.detail_label.setText("")
        if self._overall is not None:
            self._overall.set_complete()
            p.overall_progress.setValue(100)
            p.overall_label.setText("总进度：全部完成 100%")
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.preview_panel.setReadOnly(False)
        elapsed = time.time() - self._start_time if self._start_time else 0
        stats_msg = f"处理完成 | 总耗时 {fmt_duration(elapsed)} | {self._stats.get('files', 0)} 个文件"
        self._add_log_entry(stats_msg)
        quality_summary = self._summarize_quality_reports()
        notify_body = f"{msg}\n{stats_msg}"
        if quality_summary:
            self._add_log_entry(quality_summary, "WARNING" if "雷" in quality_summary else "INFO")
            notify_body = f"{notify_body}\n{quality_summary}"
        system_notify("字幕工具", notify_body)
        self._update_model_status()

    def _handle_error(self, e):
        msg = e.get("message", "错误")
        self._add_log_entry(msg, "ERROR", trace=e.get("trace", ""))
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self._reset_progress()
        self.preview_panel.setReadOnly(False)
        self._update_model_status()

    def _handle_event(self, event: dict):
        handler = self._event_handlers.get(event.get("type", ""))
        if handler:
            handler(event)

    def _build_event_handlers(self) -> dict:
        """预构建事件分发表（仅一次，避免每次事件到达时重建）"""
        return {
            "log": lambda e: self._add_log_entry(e.get("message", ""), e.get("level", "INFO")),
            "transcribe_status": lambda e: self._set_elided(self.progress_panel.transcribe_label,
                f"🎤 {e.get('file','')} [{e.get('idx',0)}/{e.get('total',0)}]"),
            "file_mode": lambda e: self._overall.set_file_translation_only(e.get("idx", 0))
                if self._overall and not e.get("needs_transcribe", True) else None,
            "translate_status": lambda e: self._set_elided(self.progress_panel.translate_label,
                f"🌍 {e.get('file','')} [{e.get('idx',0)}/{e.get('total',0)}]"),
            "current": lambda e: self._set_elided(self.progress_panel.transcribe_label, f"🎤 {e.get('message', '')}"),
            "progress": self._handle_progress,
            "counter": lambda e: self.progress_panel.counter_label.setText(
                f"已转写 {e.get('generated',0)}/{e.get('total',0)} | "
                f"已翻译 {e.get('translated',0)}/{e.get('total',0)} | "
                f"缓存 {e.get('cache',0)}"),
            "language": lambda e: self.progress_panel.lang_label.setText(f"语言：{e.get('message','')}"),
            "output_path": self._handle_output_path,
            "quality_report": self._handle_quality_report,
            "pause_before_embed": self._handle_pause_before_embed,
            "preview": lambda e: self.preview_panel.set_text(e.get("message", "")),
            "preview_clear": lambda e: self.preview_panel.clear(),
            "preview_append": self._handle_preview_append,
            "done": self._handle_done,
            "error": self._handle_error,
            "model_loaded": lambda e: self._update_model_status(),
            "manual_embed_done": self._on_manual_embed_done,
            "manual_extract_done": self._on_manual_extract_done,
        }

    def _handle_output_path(self, e):
        p = Path(e.get("path", ""))
        self._output_paths.append(str(p))
        self._last_output_dir = p.parent
        # MKV 内嵌路径不跑 SRT 检查；SRT 输出若流水线未发 quality_report 则兜底检查
        if p.suffix.lower() == ".srt":
            already = any(
                (r.get("path") and Path(r["path"]).resolve() == p.resolve())
                or (r.get("name") == p.name)
                for r in self._quality_reports
            )
            if not already:
                self._check_subtitle_quality(p)

    def _handle_quality_report(self, e):
        """收集 worker 推送的结构化质量报告，供完成摘要使用。"""
        report = {
            "path": e.get("path", ""),
            "name": e.get("name", ""),
            "total_cues": e.get("total_cues", 0),
            "total_issues": e.get("total_issues", 0),
            "counts": dict(e.get("counts") or {}),
            "samples": list(e.get("samples") or []),
        }
        if e.get("backup_path"):
            report["backup_path"] = e["backup_path"]
        self._quality_reports.append(report)

    def _summarize_quality_reports(self) -> str:
        """本轮质量汇总：计数 + 是否有雷（样例已在单文件日志里）。"""
        reports = self._quality_reports
        if not reports:
            return ""
        files_with_issues = [r for r in reports if int(r.get("total_issues") or 0) > 0]
        total_issues = sum(int(r.get("total_issues") or 0) for r in reports)
        if not files_with_issues:
            return f"字幕质量：全部通过（{len(reports)} 个文件）"
        # 汇总各类计数
        merged = {"empty": 0, "too_short": 0, "too_long": 0, "overlap": 0, "gap": 0}
        for r in files_with_issues:
            for k in merged:
                merged[k] += int((r.get("counts") or {}).get(k) or 0)
        labels = {
            "empty": "空字幕", "too_short": "过短", "too_long": "过长",
            "overlap": "重叠", "gap": "间隙过长",
        }
        parts = [f"{labels[k]} {n}" for k, n in merged.items() if n]
        backup_hint = ""
        if any(r.get("backup_path") for r in files_with_issues):
            backup_hint = "；SRT 备份见 logs/srt_backup/"
        return (
            f"字幕质量：{len(files_with_issues)}/{len(reports)} 个文件有雷，"
            f"共 {total_issues} 处（" + "，".join(parts) + f"）{backup_hint}"
        )

    def _handle_pause_before_embed(self, e):
        """翻译完成后、嵌入前暂停，弹出对话框让用户预览/编辑字幕（见 dialogs.py）"""
        show_embed_confirm_dialog(self, e)

    def _handle_preview_append(self, e):
        self.preview_panel.append(e.get("message", ""))

    def _set_detail_with_eta(self, p, detail: str, pct: float):
        elapsed = time.time() - self._start_time
        remain, finish = estimate_eta(self._start_time, pct / 100)
        parts = [detail] if detail else []
        parts.extend([f"已用 {fmt_duration(elapsed)}", f"剩余 {remain}", f"预计 {finish}"])
        p.detail_label.setText(" | ".join(parts))

    def _check_subtitle_quality(self, path: Path):
        """检查字幕质量问题（兜底路径：仅当 worker 未推送 quality_report 时）"""
        if path.suffix.lower() != ".srt" or not path.exists():
            return
        report = analyze_subtitle_file(path)
        if report is None:
            return
        self._quality_reports.append(report)
        for line in format_quality_report(report):
            self._add_log_entry(line, "WARNING" if report.get("total_issues") else "INFO")

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

    # ─── 主题 ───

    def _toggle_theme(self):
        self.dark_mode = not self.dark_mode
        self.colors = DARK if self.dark_mode else LIGHT
        self._apply_style()
        self.theme_btn.setIcon(make_moon_icon() if self.dark_mode else make_sun_icon())
        self._add_log_entry(f"已切换至{'深色' if self.dark_mode else '浅色'}模式")

    def closeEvent(self, event):
        """应用关闭时释放 Whisper 模型显存、关闭本地翻译服务（含残留清理）并保存窗口状态"""
        self._closing = True  # 通知后台嵌入线程停止发信号（daemon 随进程退出）
        if self.worker.transcriber.is_loaded():
            self.worker.transcriber.release_model()
        try:
            from .local_service import shutdown_owned, shutdown_running
            shutdown_owned()    # 先关应用自己拉起的 llama-server
            shutdown_running()  # 兜底：清理 127.0.0.1:8080 上仍运行的 llama-server，避免残留占显存
        except Exception:
            pass
        self._save_window_state()
        super().closeEvent(event)

    def _confirm_peak_hours(self) -> bool:
        """DeepSeek API 高峰时段确认（仅在点击「开始处理」时调用）。

        仅当当前活跃翻译方案使用 DeepSeek 且处于高峰时段时弹窗；
        用户选「否」则返回 False 真正阻止本次处理（不再直接退出应用）。
        非 DeepSeek / 非高峰时段直接返回 True。
        """
        api_url = str((self.settings_data or {}).get("api_url", "") or "")
        if "deepseek" not in api_url.lower():
            return True
        from datetime import timezone, timedelta, datetime
        bj_tz = timezone(timedelta(hours=8))
        hour = datetime.now(bj_tz).hour
        in_peak = (9 <= hour < 12) or (14 <= hour < 18)
        if not in_peak:
            return True
        reply = QMessageBox.question(
            self, "高峰时段提醒",
            "当前为 DeepSeek API 高峰时段（9:00-12:00、14:00-18:00），价格较高。\n"
            "是否继续？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply == QMessageBox.No:
            self._add_log_entry("已取消处理（DeepSeek 高峰时段提醒）", "INFO")
            return False
        return True

    def showEvent(self, event):
        super().showEvent(event)
        # 窗口首次显示后视口宽度已确定，重新计算日志条目高度
        self.log_panel.relayout_items()

    def eventFilter(self, obj, event):
        if obj is self.log_panel.log_list and event.type() == QEvent.Resize:
            self.log_panel.relayout_items()
        return super().eventFilter(obj, event)


def main():
    import sys
    import logging
    from logging.handlers import RotatingFileHandler

    # ── 日志目录 & 文件持久化 ──
    log_dir = APP_DIR / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "subtitle_tool.log"

    file_handler = RotatingFileHandler(
        log_path, maxBytes=5 * 1024 * 1024, backupCount=3,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        handlers=[logging.StreamHandler(), file_handler],
    )

    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setApplicationName("本地字幕生成工具")

    window = SubtitleApp()
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
