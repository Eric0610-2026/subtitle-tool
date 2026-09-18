#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PySide6/Qt 版主应用窗口
"""
import logging
import time, traceback
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Dict

logger = logging.getLogger(__name__)

from PySide6.QtCore import Qt, QTimer, QEvent, QSize
from PySide6.QtGui import QAction, QFont, QIcon
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
    OverallProgress, find_tool, IGNORE_FILE,
    _read_text_auto, shift_srt_timestamps,
)
from .config import cfg
from .dialogs import SettingsDialog, show_history_dialog, show_cache_dialog, EmbedDialog, show_embed_confirm_dialog, ExtractDialog
from .muxer import embed_subtitles_to_video, extract_embedded_subtitle, convert_to_mp4
from .widgets import DropListWidget, SCAN_VIDEO_EXTS, AUDIO_EXTS
from .panels import ProgressPanel, PreviewPanel, LogPanel, SignalBridge, _silent_double_input
from .theme import load_theme_colors, make_sun_icon, make_moon_icon, detect_system_dark, build_qss
from .notifier import notify as system_notify

APP_DIR = Path(__file__).resolve().parent.parent

# ─── 配色（从 config.json 读取，见 theme.py）───
LIGHT, DARK = load_theme_colors()


def _batch_size_save_field(values: dict) -> Optional[tuple]:
    """决定「永久保存」批大小时应写入 config 的字段。

    返回 (字段名, 值)；自定义值为 None（等于当前默认）时返回 None，
    表示不覆盖 config 中原值。
    """
    bs = values.get("translation_batch_size")
    if bs is None:
        return None
    return "batch_size", bs


class SubtitleApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("🎬 本地字幕生成工具")
        # 任务栏 / Alt-Tab 图标（应用随系统主题没有原生图标时尤其明显）
        _icon_path = Path(__file__).resolve().parent / "icon.ico"
        if _icon_path.exists():
            self.setWindowIcon(QIcon(str(_icon_path)))
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
        self._loading_local_model = False  # 后台手动加载本地模型进行中
        self._last_output_dir: Optional[Path] = None  # 记录最后输出目录
        self._output_paths: List[str] = []  # 本轮所有输出文件路径
        self._stats: Dict[str, any] = {}  # 处理统计
        self._overall = None  # 跨文件总进度跟踪
        self._settings_path = Path.home() / ".subtitle_tool_settings.json"

        # ── 信号桥（替代 queue.Queue + QTimer 轮询）──
        self.signal_bridge = SignalBridge()
        self.signal_bridge.event_received.connect(self._handle_event)
        # 事件分发表只构建一次，避免每次事件到达时重建
        self._event_handlers = self._build_event_handlers()
        # 默认配置（来自 config.json）
        # 设备/精度不进设置界面，仅通过 config.json 的 whisper.device /
        # whisper.compute_type 调整
        self.settings_data = {
            "model_dir": str(APP_DIR / cfg.whisper.model_dir) if (APP_DIR / cfg.whisper.model_dir).exists() else cfg.whisper.model_dir,
            "language": cfg.whisper.language,
            "extract_audio": cfg.whisper.extract_audio,
            "vad_filter": cfg.whisper.vad_filter,
            "default_video_dir": getattr(cfg.app, "default_video_dir", ""),
            "reuse_auto_lang": getattr(cfg.whisper, "reuse_auto_lang", True),
            "target_lang": cfg.translation.target_lang,
            "translation_only": False,
            "translation_batch_size": None,  # None=跟随 config 的 batch_size 默认
            "send_all": False,
            "pause_before_embed": getattr(cfg.translation, "pause_before_embed", False),
            "backup_max_files": getattr(cfg.translation, "backup_max_files", 50),
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
        # 头部两端都是深色（navy→accent 渐变），标签固定浅色保证两种主题下都可读
        ver.setStyleSheet("color:rgba(255,255,255,0.62); font-size:11px;")
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
        self.preview_panel.connect_toolbar(self._save_preview)
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
        # 重试（断点续翻）紧挨停止：处理期间与开始按钮一并禁用
        self.retry_btn = self._make_btn(
            "🔄 重试", self._retry, object_name="bottomBtn",
            stylesheet=f"QPushButton {{ background:{self.colors['accent']}; color:white; border:none; }} "
                       "QPushButton:hover { background:#4f46e5; }")
        ar.addWidget(self.retry_btn)
        # ── 当前模型状态（信息展示：Whisper 转写模型 / 本地 Hy-MT2 翻译模型） ──
        self.model_status = QLabel("🧠 当前模型：…")
        self.model_status.setStyleSheet(f"color:{self.colors['text_muted']}; font-size:11px; padding:0 4px;")
        ar.addWidget(self.model_status)
        self.load_model_btn = self._make_btn(
            "🚀 加载本地模型", self._load_local_model, object_name="bottomBtn",
            tooltip="手动启动本地 Hy-MT2 翻译服务（127.0.0.1:8188）。"
                    "可在开始处理前预热，避免首个任务等待模型加载；也可用于排查本地服务启动问题")
        ar.addWidget(self.load_model_btn)
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
        dlg = SettingsDialog(self, self.settings_data,
                             history_cb=self._show_history, cache_cb=self._show_cache)
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
        raw["whisper"]["extract_audio"] = values.get("extract_audio", True)
        raw["whisper"]["vad_filter"] = values.get("vad_filter", True)
        raw["whisper"]["reuse_auto_lang"] = values.get("reuse_auto_lang", True)
        trans = raw.setdefault("translation", {})
        trans["target_lang"] = values.get("target_lang", "zh")
        _bs_save = _batch_size_save_field(values)
        if _bs_save is not None:  # 自定义值才写入 config.json；默认值不覆盖
            field, _bs_val = _bs_save
            trans[field] = _bs_val
        trans["pause_before_embed"] = values.get("pause_before_embed", False)
        trans["backup_max_files"] = values.get("backup_max_files", 50)
        try:
            path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as e:
            QMessageBox.warning(self, "保存失败", f"写入 config.json 失败：{e}")
            return
        # 仅刷新运行期动态读取的配置（batch_size、并发数等）；各模块 import 时
        # 固化的模块级常量（API 超时、扩展名表、主题色等）需重启应用才生效
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
                self.preview_panel.set_text(_read_text_auto(c))
                self.preview_panel.last_output_dir = c.parent
                # 绑定内容来源：保存时回写同一文件，避免按当前选中项推导目标
                # 导致"预览的是 A 文件、保存却覆盖 B 文件"
                self.preview_panel.source_path = c
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
        new_content = shift_srt_timestamps(content, offset)
        self.preview_panel.set_text(new_content)
        self._add_log_entry(f"时间偏移 {offset:+.1f}s（预览区）")
        # 自动保存
        self._save_preview()

    def _save_preview(self):
        """保存预览区修改到来源 SRT（优先用加载时绑定的文件路径）"""
        bound = self.preview_panel.source_path
        if bound is not None:
            srt_path = bound
        else:
            # 无绑定（如转写实时预览）：退回按当前选中项推导目标，
            # 先弹确认避免静默覆盖无关字幕文件
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
            if not self._confirm("保存预览",
                                 f"预览内容未绑定到具体字幕文件，将写入：\n{srt_path}\n\n确定覆盖吗？",
                                 default_no=True):
                return
        try:
            srt_path.write_text(self.preview_panel.get_text(), encoding="utf-8")
            self._add_log_entry(f"已保存预览修改：{srt_path.name}")
        except OSError as e:
            QMessageBox.warning(self, "保存失败", str(e))

    # ─── 处理控制 ───

    def _build_opts(self, skip_completed=False):
        s = self.settings_data
        return {
            "work_dir": self.work_dir,
            "model_dir": s.get("model_dir", ""),
            "language": s.get("language", "auto"),
            "target_lang": s.get("target_lang", "zh"),
            "device": cfg.whisper.device,
            "compute_type": cfg.whisper.compute_type,
            "translate_enabled": self.trans_cb.isChecked(),
            "extract_audio": s.get("extract_audio", True),
            "vad_filter": s.get("vad_filter", True),
            "reuse_auto_lang": s.get("reuse_auto_lang", True),
            "translation_only": s.get("translation_only", False),
            "translation_batch_size": s.get("translation_batch_size"),
            "send_all": s.get("send_all", False),
            "pause_before_embed": s.get("pause_before_embed", False),
            "skip_completed": skip_completed,
            "post": self.signal_bridge.post,
            "_is_stopped": lambda: self.worker.stop_requested,
            "_register_proc": self.worker._register_proc,
            "_unregister_proc": self.worker._unregister_proc,
        }

    def _begin_processing(self, jobs, opts, log_msg):
        self._start_time = time.time()
        self._stats = {"files": len(jobs)}
        self._output_paths = []
        self.start_btn.setEnabled(False)
        self.retry_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self._reset_progress()
        self.preview_panel.clear()
        self._add_log_entry(log_msg)
        w = getattr(cfg.progress, "transcribe_weight", 80.0) if hasattr(cfg, "progress") else 80.0
        self._overall = OverallProgress(len(jobs), transcribe_weight=w)
        self._overall.start()
        self.progress_panel.overall_progress.setValue(0)
        self.progress_panel.overall_label.setText(f"第 1/{len(jobs)} 个 · 已完成 0%")
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
            # 防御性复位：worker 线程若已异常退出而未发 done/error（理论上
            # pipeline 顶层已兜底），按钮会卡在"运行中"；此时直接恢复可操作状态
            if self.worker.thread is not None and self.stop_btn.isEnabled():
                self.start_btn.setEnabled(True)
                self.retry_btn.setEnabled(True)
                self.stop_btn.setEnabled(False)
                self.preview_panel.setReadOnly(False)
                self._add_log_entry("处理线程已退出，已复位界面状态", "WARNING")
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

        # 本地翻译模型：服务运行中才显示
        if is_service_running():
            parts.append("本地模型 Hy-MT2")

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

    # ─── 手动加载本地模型 ───

    def _load_local_model(self):
        """手动拉起本地 Hy-MT2 翻译服务（llama-server），后台线程执行。

        用于在开始处理前预热模型，或排查本地服务启动问题；
        与翻译阶段的自动拉起共用 ensure_running（幂等，已运行时直接返回）。
        """
        if getattr(self, "_loading_local_model", False):
            QMessageBox.warning(self, "提示", "本地模型正在加载中，请等待完成")
            return
        if self.worker.thread and self.worker.thread.is_alive():
            QMessageBox.warning(self, "提示", "主任务正在处理中，请先停止再加载本地模型")
            return
        from .local_service import is_service_running
        if is_service_running():
            self._update_model_status()
            QMessageBox.information(self, "本地模型", "本地翻译服务已在运行，无需重复加载")
            return

        self._loading_local_model = True
        self.load_model_btn.setEnabled(False)
        self._add_log_entry("🚀 开始加载本地 Hy-MT2 模型（首次加载通常需 10~60 秒）...")
        import threading
        threading.Thread(target=self._load_local_model_worker, daemon=True).start()

    def _load_local_model_worker(self) -> None:
        """后台加载线程：调用 ensure_running 拉起/等待本地服务，结果经信号桥回传。

        异常路径也要保证发送 local_model_loaded 复位按钮状态，避免
        _loading_local_model 永久锁死加载功能；窗口关闭（_closing）后
        事件由 _handle_event 统一忽略，daemon 线程随进程退出。
        """
        ok = False
        detail = ""
        try:
            from .local_service import ensure_running
            ok, detail, _first = ensure_running(on_progress=lambda sec: self.signal_bridge.post({
                "type": "log",
                "message": f"正在加载本地模型… 已等待 {sec}s（首次加载通常 10~60 秒）",
            }))
            level = "INFO" if ok else "ERROR"
            self.signal_bridge.post({
                "type": "log",
                "message": (f"✅ {detail}" if ok else f"❌ 本地模型加载失败：{detail}"),
                "level": level,
            })
        except Exception as e:
            logger.error("加载本地模型线程异常: %s\n%s", e, traceback.format_exc())
            detail = str(e)
            try:
                self.signal_bridge.post({
                    "type": "log", "message": f"❌ 加载本地模型失败: {e}", "level": "ERROR",
                })
            except Exception:
                pass
        finally:
            try:
                self.signal_bridge.post({
                    "type": "local_model_loaded", "ok": ok, "detail": detail,
                })
            except Exception:
                pass

    def _on_local_model_loaded(self, e):
        """本地模型加载结束：复位按钮、刷新模型状态，并弹窗提示结果"""
        self._loading_local_model = False
        self.load_model_btn.setEnabled(True)
        self._update_model_status()
        if getattr(self, "_closing", False):
            return
        if e.get("ok"):
            QMessageBox.information(self, "本地模型", "✅ 本地 Hy-MT2 翻译服务已就绪，可以开始处理了")
        else:
            QMessageBox.warning(self, "本地模型加载失败",
                                f"{e.get('detail') or '未知错误'}\n\n详情请查看日志。")

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
                f"  3. 按需修改 {config_path.name} 中的识别语言、模型目录、批量大小等参数\n\n"
                "如果没有 config.json，应用会加载默认配置运行，但「永久保存」按钮不可用。\n"
                "（仍可通过「本次有效」按钮在当前会话中使用所有功能。）"
            )
            self._add_log_entry(
                f"未找到 {config_path.name}，已从 {config_example.name} 加载默认配置。"
                f"请复制为 {config_path.name} 以启用「永久保存」", "WARNING")
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
        # 条会停在最后一段 seg_end/duration）
        if stage in ("提取音频", "加载模型", "读取字幕", "转写中", "转写完成"):
            bar_pct = 100 if stage == "转写完成" else int(pct)
            p.stage_bar.setValue(bar_pct)
            p.stage_bar.setFormat(f"转写 {bar_pct}%")
            if detail:
                p.stage_detail.setText(detail)
        elif stage == "翻译":
            # 串行两阶段：进入翻译说明转写阶段已结束，条从头展示翻译进度
            p.stage_bar.setValue(int(pct))
            p.stage_bar.setFormat(f"翻译 {int(pct)}%")
            p.stage_detail.setText(detail)
            # 翻译阶段已开始：本地模型可能已加载，刷新「当前模型」标签（不再是 whisper）
            if not self._model_status_refreshed:
                self._model_status_refreshed = True
                self._update_model_status()
        elif stage in ("组织输出", "完成", "跳过"):
            p.stage_bar.setValue(100)
            p.stage_bar.setFormat("100%")
        # 底部 ETA 行：所有进度事件都刷新已用/剩余/预计；stage 详情只写 stage_detail，不重复到底部
        if stage in ("提取音频", "加载模型", "读取字幕", "转写中", "转写完成", "翻译"):
            if self._start_time and pct:
                self._set_detail_with_eta(p, "", pct)
            else:
                p.detail_label.setText("")
        elif detail and self._start_time and pct:
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
            p.overall_label.setText(
                f"第 {idx}/{self._overall.total} 个 · 已完成 {overall_pct:.0f}%")

    def _handle_done(self, e):
        p = self.progress_panel
        msg = e.get("message", "完成")
        stopped = e.get("stopped", False)
        self._add_log_entry(msg, "INFO")
        if not stopped:
            p.stage_bar.setValue(100)
            p.stage_bar.setFormat("100%")
            p.detail_label.setText("")
            if self._overall is not None:
                self._overall.set_complete()
                p.overall_progress.setValue(100)
                p.overall_label.setText("全部完成 100%")
        else:
            # 用户主动停止：保留当前进度，不谎报"全部完成"
            p.detail_label.setText("")
            if self._overall is not None:
                p.overall_label.setText("已停止（部分完成）")
        self.start_btn.setEnabled(True)
        self.retry_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.preview_panel.setReadOnly(False)
        elapsed = time.time() - self._start_time if self._start_time else 0
        if stopped:
            stats_msg = f"处理已停止 | 总耗时 {fmt_duration(elapsed)} | {self._stats.get('files', 0)} 个文件"
        else:
            stats_msg = f"处理完成 | 总耗时 {fmt_duration(elapsed)} | {self._stats.get('files', 0)} 个文件"
        self._add_log_entry(stats_msg)
        notify_body = f"{msg}\n{stats_msg}"
        system_notify("字幕工具", notify_body)
        self._update_model_status()

    def _handle_error(self, e):
        msg = e.get("message", "错误")
        self._add_log_entry(msg, "ERROR", trace=e.get("trace", ""))
        self.start_btn.setEnabled(True)
        self.retry_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self._reset_progress()
        self.preview_panel.setReadOnly(False)
        self._update_model_status()

    def _handle_event(self, event: dict):
        if self._closing:
            return  # 窗口已开始关闭：忽略迟到事件，避免操作已销毁的控件
        handler = self._event_handlers.get(event.get("type", ""))
        if handler:
            handler(event)

    def _build_event_handlers(self) -> dict:
        """预构建事件分发表（仅一次，避免每次事件到达时重建）"""
        return {
            "log": lambda e: self._add_log_entry(e.get("message", ""), e.get("level", "INFO")),
            "transcribe_status": lambda e: self._set_elided(self.progress_panel.stage_label,
                f"🎤 转写 {e.get('file','')} [{e.get('idx',0)}/{e.get('total',0)}]"),
            "file_mode": lambda e: self._overall.set_file_translation_only(e.get("idx", 0))
                if self._overall and not e.get("needs_transcribe", True) else None,
            "translate_status": lambda e: self._set_elided(self.progress_panel.stage_label,
                f"🌍 翻译 {e.get('file','')} [{e.get('idx',0)}/{e.get('total',0)}]"),
            "current": lambda e: self._set_elided(self.progress_panel.stage_label, f"🎤 {e.get('message', '')}"),
            "progress": self._handle_progress,
            "counter": lambda e: self.progress_panel.counter_label.setText(
                f"已转写 {e.get('generated',0)}/{e.get('total',0)} | "
                f"已翻译 {e.get('translated',0)}/{e.get('total',0)} | "
                f"缓存 {e.get('cache',0)}"),
            "language": lambda e: self.progress_panel.lang_label.setText(f"语言：{e.get('message','')}"),
            "output_path": self._handle_output_path,
            "pause_before_embed": self._handle_pause_before_embed,
            "preview": self._handle_preview_live,
            "preview_clear": lambda e: self.preview_panel.clear(),
            "preview_append": self._handle_preview_append,
            "done": self._handle_done,
            "error": self._handle_error,
            "model_loaded": lambda e: self._update_model_status(),
            "manual_embed_done": self._on_manual_embed_done,
            "manual_extract_done": self._on_manual_extract_done,
            "local_model_loaded": self._on_local_model_loaded,
        }

    def _handle_output_path(self, e):
        p = Path(e.get("path", ""))
        self._output_paths.append(str(p))
        self._last_output_dir = p.parent

    def _handle_pause_before_embed(self, e):
        """翻译完成后、嵌入前暂停，弹出对话框让用户预览/编辑字幕（见 dialogs.py）"""
        show_embed_confirm_dialog(self, e)

    def _handle_preview_live(self, e):
        # 实时预览内容与之前绑定文件的对应关系已失效，解绑避免保存覆盖旧文件
        self.preview_panel.source_path = None
        self.preview_panel.set_text(e.get("message", ""))

    def _handle_preview_append(self, e):
        self.preview_panel.source_path = None
        self.preview_panel.append(e.get("message", ""))

    def _set_detail_with_eta(self, p, detail: str, pct: float):
        elapsed = time.time() - self._start_time
        # ETA 统一用 OverallProgress 的整体进度估算：按当前子阶段百分比
        # 估算全局剩余时间在前半程会严重失真
        if self._overall is not None:
            remain, finish = self._overall.eta()
        else:
            remain, finish = estimate_eta(self._start_time, pct / 100)
        parts = [detail] if detail else []
        parts.extend([f"已用 {fmt_duration(elapsed)}", f"剩余 {remain}", f"预计 {finish}"])
        p.detail_label.setText(" | ".join(parts))

    # ─── 主题 ───

    def _toggle_theme(self):
        self.dark_mode = not self.dark_mode
        self.colors = DARK if self.dark_mode else LIGHT
        self._apply_style()
        # 表格单元格的 QBrush 颜色不随 QSS 切换，需重渲染才不会残留旧主题配色
        self.preview_panel.refresh_theme()
        self.theme_btn.setIcon(make_moon_icon() if self.dark_mode else make_sun_icon())
        self._add_log_entry(f"已切换至{'深色' if self.dark_mode else '浅色'}模式")

    def closeEvent(self, event):
        """应用关闭时停止处理线程、释放 Whisper 模型显存、关闭本会话拉起的本地翻译服务并保存窗口状态"""
        self._closing = True  # 通知后台线程停止发信号（daemon 随进程退出）
        # 先停 worker：终止 ffmpeg 等子进程并置停止标志，避免与下方 release_model 竞争
        self.worker.stop()
        if self.worker.transcriber.is_loaded():
            self.worker.transcriber.release_model()
        try:
            # 只关本会话拉起的 llama-server；用户手动启动的外部服务不强制杀
            from .local_service import shutdown_owned
            shutdown_owned()
        except Exception:
            pass
        self._save_window_state()
        super().closeEvent(event)

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
    # 统一现代中文字体（Windows 默认 UI 字体对中文场景偏旧）；具体控件里的
    # Consolas 等由 setFont 单独指定，仍会覆盖应用级默认
    base_font = QFont()
    base_font.setFamilies(["Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI"])
    app.setFont(base_font)

    window = SubtitleApp()
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
