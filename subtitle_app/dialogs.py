#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
对话框模块：设置、历史管理、缓存管理
"""
from pathlib import Path
from typing import List

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QGridLayout, QGroupBox,
    QLineEdit, QComboBox, QCheckBox, QPushButton, QListWidget,
    QListWidgetItem, QLabel, QSpinBox, QFileDialog, QMessageBox,
    QAbstractItemView, QTabWidget, QWidget, QFrame, QTextEdit,
)
from PySide6.QtCore import Qt, QThread, Signal, QObject, QSize
from PySide6.QtGui import QFont, QPalette, QIcon, QPainter, QPen, QPixmap, QColor

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


def _circle_symbol_icon(symbol: str, size: int = 18) -> QIcon:
    """绘制圆形加号/减号图标（不依赖系统 emoji 字体）"""
    def _draw(bg: QColor, fg: QColor) -> QPixmap:
        pm = QPixmap(size, size)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(bg)
        margin = 1
        p.drawEllipse(margin, margin, size - 2 * margin, size - 2 * margin)
        pen = QPen(fg, 2)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        c = size / 2
        arm = size / 2 - 3.5
        p.drawLine(c - arm, c, c + arm, c)  # 横线
        if symbol == "+":
            p.drawLine(c, c - arm, c, c + arm)  # 竖线
        p.end()
        return pm

    icon = QIcon()
    icon.addPixmap(_draw(QColor("#6366f1"), QColor("white")),
                   QIcon.Mode.Normal, QIcon.State.Off)
    icon.addPixmap(_draw(QColor("#cbd5e1"), QColor("#94a3b8")),
                   QIcon.Mode.Disabled, QIcon.State.Off)
    return icon


class ApiTestWorker(QObject):
    """后台线程：测试 API 连接是否可用"""
    finished = Signal(str)  # 返回结果消息

    def __init__(self, api_url: str, api_key: str, model: str, target_lang: str = "zh"):
        super().__init__()
        self.api_url = api_url
        self.api_key = api_key
        self.model = model
        self.target_lang = target_lang

    def run(self):
        """发送一条测试请求，验证 API 配置是否有效"""
        # 构造简单的测试请求——只发一条简短翻译，确认 API 返回可用
        import json, urllib.request, urllib.error
        test_text = "Hello"
        prompt = (
            f"你是严谨的字幕翻译器。将以下数组中的字幕文本逐条翻译为{self.target_lang}。"
            "要求：\n"
            f"1. 译文符合{self.target_lang}表达习惯，自然流畅\n"
            "2. 返回格式严格为 JSON 数组，每个元素为对应译文\n"
            f'示例：["你好"]'
        )
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": prompt},
                {"role": "user", "content": json.dumps([test_text], ensure_ascii=False)},
            ],
            "temperature": 0.1,
            "stream": False,
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(self.api_url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                resp_json = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")[:200]
            self.finished.emit(f"❌ HTTP {e.code}: {body}")
            return
        except urllib.error.URLError as e:
            self.finished.emit(f"❌ 网络错误: {e.reason}")
            return
        except TimeoutError:
            self.finished.emit("❌ 请求超时（30s）")
            return
        except json.JSONDecodeError:
            self.finished.emit("❌ 响应不是有效的 JSON")
            return
        except Exception as e:
            self.finished.emit(f"❌ 请求失败: {e}")
            return

        # 检查响应
        err = resp_json.get("error")
        if err:
            msg = err.get("message", "") if isinstance(err, dict) else str(err)
            self.finished.emit(f"❌ API 返回错误: {msg[:120]}")
            return

        choices = resp_json.get("choices", [])
        if not choices:
            self.finished.emit("❌ 响应缺少 choices 字段")
            return

        content = ""
        if isinstance(choices[0], dict):
            msg = choices[0].get("message", {})
            if isinstance(msg, str):
                content = msg
            elif isinstance(msg, dict):
                content = msg.get("content", "")

        if not content:
            self.finished.emit("❌ 响应内容为空")
            return

        # 模型名称
        model_name = resp_json.get("model", "") or self.model
        self.finished.emit(f"✅ 检测成功（模型: {model_name}）")


class ModelListWorker(QObject):
    """后台线程：获取 API 可用模型列表"""
    finished = Signal(list)  # 返回模型 ID 列表
    error = Signal(str)      # 返回错误消息

    def __init__(self, api_url: str, api_key: str):
        super().__init__()
        self.api_url = api_url
        self.api_key = api_key

    @staticmethod
    def derive_models_url(api_url: str) -> str:
        """从 API URL 推导 models 端点 URL"""
        url = api_url.rstrip("/")
        # 标准 OpenAI 兼容格式：/v1/chat/completions → /v1/models
        if url.endswith("/chat/completions"):
            return url.replace("/chat/completions", "/models")
        # 如果包含 /v1/ 但路径不同，尝试同级替换
        if "/v1/" in url:
            return url.rsplit("/", 1)[0] + "/models"
        # 兜底
        return url + "/models"

    def run(self):
        """调用 /v1/models 端点获取可用模型列表"""
        import json, urllib.request, urllib.error
        models_url = self.derive_models_url(self.api_url)
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        req = urllib.request.Request(models_url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                resp_json = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")[:200]
            self.error.emit(f"HTTP {e.code}: {body}")
            return
        except urllib.error.URLError as e:
            self.error.emit(f"网络错误: {e.reason}")
            return
        except TimeoutError:
            self.error.emit("请求超时（15s）")
            return
        except json.JSONDecodeError:
            self.error.emit("响应不是有效的 JSON")
            return
        except Exception as e:
            self.error.emit(f"请求失败: {e}")
            return

        # 提取模型列表
        err = resp_json.get("error")
        if err:
            msg = err.get("message", "") if isinstance(err, dict) else str(err)
            self.error.emit(f"API 返回错误: {msg[:120]}")
            return

        models_data = resp_json.get("data", resp_json)
        if isinstance(models_data, list):
            model_ids = []
            for m in models_data:
                if isinstance(m, dict):
                    mid = m.get("id", "")
                elif isinstance(m, str):
                    mid = m
                else:
                    continue
                if mid:
                    model_ids.append(mid)
            if model_ids:
                model_ids.sort()
                self.finished.emit(model_ids)
            else:
                self.error.emit("模型列表为空")
        else:
            self.error.emit("响应格式不符合预期（缺少 data 数组）")


class ModelConfigDialog(QDialog):
    """二级对话框：模型配置（API URL/Key + 模型选择/搜索/检测）"""

    def __init__(self, parent, api_url: str = "", api_key: str = "",
                 current_model: str = "", target_lang: str = "zh"):
        super().__init__(parent)
        self.setWindowTitle("模型配置")
        self.setMinimumSize(520, 520)
        self.resize(560, 560)
        self._api_url = api_url
        self._api_key = api_key
        self._target_lang = target_lang
        self._all_models: List[str] = []

        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        # ── API URL ──
        layout.addWidget(QLabel("API URL"))
        self.api_url_edit = QLineEdit(api_url)
        self.api_url_edit.setPlaceholderText("https://api.example.com/v1/chat/completions")
        self.api_url_edit.textChanged.connect(self._on_field_changed)
        layout.addWidget(self.api_url_edit)

        # ── API Key ──
        layout.addWidget(QLabel("API Key"))
        key_row = QHBoxLayout()
        self.api_key_edit = QLineEdit(api_key)
        self.api_key_edit.setEchoMode(QLineEdit.Password)
        self.api_key_edit.setPlaceholderText("sk-...")
        self.api_key_edit.textChanged.connect(self._on_field_changed)
        key_row.addWidget(self.api_key_edit, 1)
        self.show_key_btn = QPushButton("显示")
        # 不锁死宽度：文字在「显示/隐藏」间切换时能完整显示（固定宽度会截断）
        self.show_key_btn.setMinimumWidth(52)
        self.show_key_btn.setToolTip("显示/隐藏 API Key")
        self.show_key_btn.setCheckable(True)
        self.show_key_btn.clicked.connect(self._toggle_key_visible)
        key_row.addWidget(self.show_key_btn)
        layout.addLayout(key_row)

        # ── 搜索框 + 获取列表 ──
        search_row = QHBoxLayout()
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("🔍 搜索模型...")
        self.search_edit.textChanged.connect(self._apply_filter)
        search_row.addWidget(self.search_edit, 1)
        self.fetch_btn = QPushButton("📋 获取列表")
        self.fetch_btn.setObjectName("accentBtn")
        self.fetch_btn.setToolTip("从 API 获取可用模型列表")
        self.fetch_btn.clicked.connect(self._fetch_models)
        search_row.addWidget(self.fetch_btn)
        layout.addLayout(search_row)

        # ── 模型列表 ──
        self.model_list = QListWidget()
        self.model_list.setObjectName("modelList")
        self.model_list.setAlternatingRowColors(True)
        dark_mode = self.palette().color(QPalette.Window).lightness() < 128
        if dark_mode:
            list_bg = "#202535"
            alt_bg = "#252b3d"
            text_fg = "#dbe4f0"
            border = "#3b455b"
            hover_bg = "#303951"
            selected_bg = "#4f5f9f"
        else:
            list_bg = "#ffffff"
            alt_bg = "#f1f5f9"
            text_fg = "#172033"
            border = "#cbd5e1"
            hover_bg = "#e0e7ff"
            selected_bg = "#4f46e5"
        self.model_list.setStyleSheet(f"""
            QListWidget#modelList {{
                background: {list_bg}; color: {text_fg};
                alternate-background-color: {alt_bg};
                selection-background-color: {selected_bg};
                selection-color: #ffffff; border: 1px solid {border};
            }}
            QListWidget#modelList::item {{
                color: {text_fg}; background: {list_bg}; padding: 6px 8px;
            }}
            QListWidget#modelList::item:alternate {{
                color: {text_fg}; background: {alt_bg};
            }}
            QListWidget#modelList::item:hover {{
                color: {text_fg}; background: {hover_bg};
            }}
            QListWidget#modelList::item:selected {{
                color: #ffffff; background: {selected_bg};
            }}
        """)
        self.model_list.itemClicked.connect(self._on_item_clicked)
        self.model_list.itemDoubleClicked.connect(lambda item: self.accept())
        layout.addWidget(self.model_list, 1)

        # ── 手动输入 + 检测 ──
        manual_row = QHBoxLayout()
        manual_row.addWidget(QLabel("或手动输入:"))
        self.manual_edit = QLineEdit()
        self.manual_edit.setPlaceholderText("输入模型名称...")
        manual_row.addWidget(self.manual_edit, 1)
        self.test_btn = QPushButton("🔍 检测")
        self.test_btn.setObjectName("accentBtn")
        self.test_btn.setToolTip("向 API 发送一条测试请求，验证选中模型是否可用")
        self.test_btn.clicked.connect(self._test_selected)
        manual_row.addWidget(self.test_btn)
        layout.addLayout(manual_row)

        # ── 状态 ──
        self.status_label = QLabel("")
        self.status_label.setStyleSheet("color:#64748b; font-size:11px;")
        layout.addWidget(self.status_label)

        # ── 底部按钮 ──
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        cancel_btn = QPushButton("取消")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)
        ok_btn = QPushButton("确定")
        ok_btn.setObjectName("startBtn")
        ok_btn.clicked.connect(self.accept)
        btn_row.addWidget(ok_btn)
        layout.addLayout(btn_row)

        # 预填当前模型
        if current_model:
            self._all_models = [current_model]
            self._apply_filter("")
            self.manual_edit.setText(current_model)

        # 自动获取（有 URL 和 Key 时）
        if api_url and api_key:
            self._fetch_models()

    def _on_field_changed(self):
        """字段变化时清空状态"""
        self.status_label.setText("")

    def _toggle_key_visible(self, checked: bool):
        """切换 API Key 明文/密文显示"""
        self.api_key_edit.setEchoMode(
            QLineEdit.Normal if checked else QLineEdit.Password)
        self.show_key_btn.setText("隐藏" if checked else "显示")

    # ── 搜索过滤 ──

    def _apply_filter(self, text: str):
        self.model_list.clear()
        keyword = text.strip().lower()
        for mid in self._all_models:
            if not keyword or keyword in mid.lower():
                self.model_list.addItem(mid)

    def _on_item_clicked(self, item):
        """列表点击时同步到手动输入框"""
        self.manual_edit.setText(item.text())

    # ── 获取模型列表 ──

    def _fetch_models(self):
        api_url = self.api_url_edit.text().strip()
        api_key = self.api_key_edit.text().strip()
        if not api_url:
            self.status_label.setText("⚠ 请先填写 API URL")
            self.status_label.setStyleSheet("color:#eab308; font-size:11px;")
            return
        if not api_key:
            self.status_label.setText("⚠ 请先填写 API Key")
            self.status_label.setStyleSheet("color:#eab308; font-size:11px;")
            return

        self.fetch_btn.setEnabled(False)
        self.fetch_btn.setText("⏳ 获取中...")
        self.status_label.setText("⏳ 正在获取模型列表...")
        self.status_label.setStyleSheet("color:#64748b; font-size:11px;")

        # 请求序号：若旧请求仍在飞行，其迟到回调会被忽略，避免竞态覆盖
        self._fetch_seq = getattr(self, "_fetch_seq", 0) + 1
        seq = self._fetch_seq

        self._thread = QThread()
        self._worker = ModelListWorker(api_url, api_key)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.finished.connect(lambda ids, s=seq: self._on_models_fetched(ids, s))
        self._worker.error.connect(lambda msg, s=seq: self._on_models_error(msg, s))
        self._worker.finished.connect(self._thread.quit)
        self._worker.error.connect(self._thread.quit)
        self._worker.finished.connect(self._worker.deleteLater)
        self._worker.error.connect(self._worker.deleteLater)
        self._thread.finished.connect(self._thread.deleteLater)
        self._thread.start()

    def _on_models_fetched(self, model_ids: list, seq: int = None):
        if seq is not None and seq != getattr(self, "_fetch_seq", None):
            return  # 已发起更新的请求，忽略过期结果
        self.fetch_btn.setEnabled(True)
        self.fetch_btn.setText("📋 获取列表")
        self._all_models = list(model_ids)
        self._apply_filter(self.search_edit.text())
        self.status_label.setText(f"✅ 获取到 {len(model_ids)} 个模型")
        self.status_label.setStyleSheet("color:#22c55e; font-size:11px;")

    def _on_models_error(self, err_msg: str, seq: int = None):
        if seq is not None and seq != getattr(self, "_fetch_seq", None):
            return  # 已发起更新的请求，忽略过期结果
        self.fetch_btn.setEnabled(True)
        self.fetch_btn.setText("📋 获取列表")
        self.status_label.setText(f"❌ {err_msg}")
        self.status_label.setStyleSheet("color:#ef4444; font-size:11px;")

    # ── 检测 ──

    def _test_selected(self):
        model = self.selected_model()
        api_url = self.api_url_edit.text().strip()
        api_key = self.api_key_edit.text().strip()
        if not api_url or not api_key:
            self.status_label.setText("⚠ 请先填写 API URL 和 Key")
            self.status_label.setStyleSheet("color:#eab308; font-size:11px;")
            return
        if not model:
            self.status_label.setText("⚠ 请先选择或输入模型")
            self.status_label.setStyleSheet("color:#eab308; font-size:11px;")
            return

        self.test_btn.setEnabled(False)
        self.test_btn.setText("⏳ 检测中...")
        self.status_label.setText("⏳ 正在连接 API...")
        self.status_label.setStyleSheet("color:#64748b; font-size:11px;")

        # 请求序号：旧请求的迟到回调将被忽略，避免竞态覆盖
        self._test_seq = getattr(self, "_test_seq", 0) + 1
        tseq = self._test_seq

        self._test_thread = QThread()
        self._test_worker = ApiTestWorker(api_url, api_key, model, self._target_lang)
        self._test_worker.moveToThread(self._test_thread)
        self._test_thread.started.connect(self._test_worker.run)
        self._test_worker.finished.connect(lambda r, s=tseq: self._on_test_done(r, s))
        self._test_worker.finished.connect(self._test_thread.quit)
        self._test_worker.finished.connect(self._test_worker.deleteLater)
        self._test_thread.finished.connect(self._test_thread.deleteLater)
        self._test_thread.start()

    def _on_test_done(self, result: str, seq: int = None):
        if seq is not None and seq != getattr(self, "_test_seq", None):
            return  # 已发起更新的请求，忽略过期结果
        self.test_btn.setEnabled(True)
        self.test_btn.setText("🔍 检测")
        is_success = result.startswith("✅")
        self.status_label.setText(result)
        if is_success:
            self.status_label.setStyleSheet("color:#22c55e; font-size:11px;")
        else:
            self.status_label.setStyleSheet("color:#ef4444; font-size:11px;")

    # ── 结果 ──

    def selected_model(self) -> str:
        item = self.model_list.currentItem()
        if item:
            return item.text().strip()
        manual = self.manual_edit.text().strip()
        return manual

    def get_values(self) -> dict:
        return {
            "api_url": self.api_url_edit.text().strip(),
            "api_key": self.api_key_edit.text().strip(),
            "model": self.selected_model(),
        }


class SettingsDialog(QDialog):
    """二级设置对话框——语音识别 + AI翻译全部参数"""

    def __init__(self, parent, values: dict):
        super().__init__(parent)
        self.setStyleSheet(_SCROLLBAR_STYLE + """
            QCheckBox { background: transparent; spacing: 7px; }
            QCheckBox::indicator { width: 16px; height: 16px; }
        """)
        self.setWindowTitle("更多设置")
        self.setMinimumWidth(520)
        self._model_name = values.get("translation_model", "")
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
        opts_row.setSpacing(18)
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
        r += 1

        # ── 默认视频目录（个性化：用于「📌 默认」按钮与字幕页自动扫描）──
        g1.addWidget(QLabel("默认视频目录"), r, 0)
        self.default_dir = QLineEdit(values.get("default_video_dir", ""))
        self.default_dir.setPlaceholderText("可留空；用于「📌 默认」按钮与字幕页自动扫描")
        g1.addWidget(self.default_dir, r, 1)
        dir_browse_btn = QPushButton("浏览...")
        dir_browse_btn.clicked.connect(lambda: self.default_dir.setText(
            QFileDialog.getExistingDirectory(self, "选择默认视频目录", self.default_dir.text())))
        g1.addWidget(dir_browse_btn, r, 2)
        r += 1

        # ── 语言检测复用开关 ──
        self.reuse_lang_cb = QCheckBox("复用同批语言检测结果（单一语言目录更快）")
        self.reuse_lang_cb.setChecked(values.get("reuse_auto_lang", False))
        self.reuse_lang_cb.setToolTip("勾选后：auto 模式下第一个文件的检测语言将复用到同批后续文件，"
                                      "跳过重复检测；混合语言目录请保持关闭")
        g1.addWidget(self.reuse_lang_cb, r, 0, 1, 3)
        layout.addWidget(sg1)

        # ── AI 翻译（多方案管理）──
        sg2 = QGroupBox("🌍 AI 翻译")
        g2 = QGridLayout(sg2)
        g2.setVerticalSpacing(8)

        # ── 初始化方案数据 ──
        self._presets = values.get("presets")
        if not self._presets:
            self._presets = [{
                "id": "default",
                "name": "默认方案",
                "api_url": values.get("api_url", ""),
                "api_key": values.get("api_key", ""),
                "model": values.get("translation_model", ""),
            }]
        self._active_id = values.get("active_preset", self._presets[0]["id"])
        self._updating = False  # 防止信号递归

        r = 0
        g2.addWidget(QLabel("目标语言"), r, 0)
        self.target_lang = QComboBox()
        self.target_lang.addItems(["zh", "en", "ja", "ko", "fr", "de", "es", "ru"])
        self.target_lang.setCurrentText(values.get("target_lang", "zh"))
        g2.addWidget(self.target_lang, r, 1, 1, 2)
        r += 1

        # ── 方案选择行 ──
        g2.addWidget(QLabel("方案"), r, 0)
        preset_row = QHBoxLayout()
        preset_row.setSpacing(4)
        self.preset_combo = QComboBox()
        self.preset_combo.currentIndexChanged.connect(self._on_preset_selected)
        preset_row.addWidget(self.preset_combo, 1)
        add_btn = QPushButton()
        add_btn.setFixedWidth(32)
        add_btn.setIcon(_circle_symbol_icon("+"))
        add_btn.setIconSize(QSize(18, 18))
        add_btn.setToolTip("添加新方案")
        add_btn.clicked.connect(self._add_preset)
        preset_row.addWidget(add_btn)
        self.del_btn = QPushButton()
        self.del_btn.setFixedWidth(32)
        self.del_btn.setIcon(_circle_symbol_icon("-"))
        self.del_btn.setIconSize(QSize(18, 18))
        self.del_btn.setToolTip("删除当前方案")
        self.del_btn.clicked.connect(self._del_preset)
        preset_row.addWidget(self.del_btn)
        preset_row.addStretch()
        g2.addLayout(preset_row, r, 1, 1, 2)
        r += 1

        # ── 当前方案的编辑字段 ──
        g2.addWidget(QLabel("方案名称"), r, 0)
        self.preset_name = QLineEdit()
        self.preset_name.textChanged.connect(self._on_field_changed)
        g2.addWidget(self.preset_name, r, 1, 1, 2)
        r += 1

        # ── 模型配置按钮（点开二级页）──
        g2.addWidget(QLabel("模型配置"), r, 0)
        cfg_row = QHBoxLayout()
        cfg_row.setSpacing(4)
        self.model_cfg_btn = QPushButton()
        self.model_cfg_btn.setObjectName("accentBtn")
        self.model_cfg_btn.setCursor(Qt.PointingHandCursor)
        self.model_cfg_btn.setStyleSheet("text-align:left; padding:6px 10px; font-size:12px;")
        self.model_cfg_btn.clicked.connect(self._open_model_config)
        cfg_row.addWidget(self.model_cfg_btn, 1)
        self.cfg_status_dot = QLabel("●")
        self.cfg_status_dot.setStyleSheet("color:#94a3b8; font-size:16px;")
        self.cfg_status_dot.setToolTip("灰色=未配置，绿色=已配置")
        cfg_row.addWidget(self.cfg_status_dot)
        g2.addLayout(cfg_row, r, 1, 1, 2)
        r += 1
        self._update_cfg_btn_text()

        # ── 其余选项 ──
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
        self.batch_size.setValue(values.get("translation_batch_size", cfg.translation.batch_size))
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

        # 填充方案下拉框
        self._rebuild_combo()

    # ── 方案管理方法 ──

    def _get_preset(self, pid: str) -> dict:
        for p in self._presets:
            if p["id"] == pid:
                return p
        return self._presets[0]

    def _get_current_preset(self) -> dict:
        return self._get_preset(self._active_id)

    def _save_current_preset(self):
        """将当前 UI 字段值刷回方案字典"""
        p = self._get_current_preset()
        p["name"] = self.preset_name.text().strip() or "未命名方案"
        p["model"] = self._model_name

    def _load_preset_fields(self, preset: dict):
        """将方案数据加载到 UI 编辑字段"""
        self.preset_name.setText(preset["name"])
        self._model_name = preset.get("model", "")
        self._update_cfg_btn_text()

    def _rebuild_combo(self):
        """重建方案下拉框（添加/删除/切换后调用）"""
        self._updating = True
        self.preset_combo.clear()
        for p in self._presets:
            if p["id"] == self._active_id:
                label = "● " + p["name"]
            else:
                label = "  " + p["name"]
            self.preset_combo.addItem(label, p["id"])
        # 选中激活的方案
        for i in range(self.preset_combo.count()):
            if self.preset_combo.itemData(i) == self._active_id:
                self.preset_combo.setCurrentIndex(i)
                break
        self._load_preset_fields(self._get_current_preset())
        self._updating = False
        self.del_btn.setEnabled(len(self._presets) > 1)
        self._update_cfg_btn_text()

    def _refresh_combo_labels(self):
        """更新下拉框文字上的星标（不重建控件）"""
        self._updating = True
        for i in range(self.preset_combo.count()):
            pid = self.preset_combo.itemData(i)
            p = self._get_preset(pid)
            if p:
                if pid == self._active_id:
                    label = "● " + p["name"]
                else:
                    label = "  " + p["name"]
                self.preset_combo.setItemText(i, label)
        self._updating = False

    def _on_preset_selected(self, idx: int):
        """切换方案：保存当前修改，加载新方案"""
        if self._updating or idx < 0:
            return
        self._save_current_preset()
        pid = self.preset_combo.itemData(idx)
        if pid and pid != self._active_id:
            self._active_id = pid
            self._updating = True
            self._load_preset_fields(self._get_preset(pid))
            self._refresh_combo_labels()
            self._updating = False
            self._update_cfg_btn_text()

    def _on_field_changed(self):
        """字段变化时自动保存到当前方案"""
        if not self._updating:
            self._save_current_preset()
            # 更新方案名称到下拉框
            idx = self.preset_combo.currentIndex()
            if idx >= 0:
                p = self._get_current_preset()
                if p["id"] == self._active_id:
                    label = "● " + p["name"]
                else:
                    label = "  " + p["name"]
                self.preset_combo.setItemText(idx, label)
            self._update_cfg_btn_text()

    def _add_preset(self):
        """添加空白新方案并选中"""
        import time
        self._save_current_preset()
        new_id = f"preset_{int(time.time())}"
        names = {p["name"] for p in self._presets}
        name = "新方案"
        if name in names:
            i = 2
            while f"{name}{i}" in names:
                i += 1
            name = f"{name}{i}"
        self._presets.append({
            "id": new_id, "name": name,
            "api_url": "", "api_key": "", "model": "",
        })
        self._active_id = new_id
        self._rebuild_combo()

    def _del_preset(self):
        """删除当前方案（至少保留一个）"""
        if len(self._presets) <= 1:
            QMessageBox.warning(self, "删除", "至少保留一个方案")
            return
        cur = self._get_current_preset()
        box = QMessageBox(self)
        box.setWindowTitle("删除方案")
        box.setText(f"确定删除方案「{cur['name']}」？")
        box.setIcon(QMessageBox.NoIcon)
        box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
        if box.exec() != QMessageBox.Yes:
            return
        self._presets = [p for p in self._presets if p["id"] != self._active_id]
        self._active_id = self._presets[0]["id"]
        self._rebuild_combo()

    def _open_model_config(self):
        """打开模型配置二级对话框"""
        self._save_current_preset()
        cur = self._get_current_preset()
        dlg = ModelConfigDialog(self, api_url=cur.get("api_url", ""),
                                api_key=cur.get("api_key", ""),
                                current_model=self._model_name,
                                target_lang=self.target_lang.currentText())
        if dlg.exec() == QDialog.Accepted:
            vals = dlg.get_values()
            cur["api_url"] = vals["api_url"]
            cur["api_key"] = vals["api_key"]
            cur["model"] = vals["model"]
            self._model_name = vals["model"]
            self._update_cfg_btn_text()

    def _update_cfg_btn_text(self):
        """更新模型配置按钮的显示文字和状态指示器"""
        cur = self._get_current_preset()
        api_url = cur.get("api_url", "").strip()
        api_key = cur.get("api_key", "").strip()
        model = self._model_name
        configured = bool(api_url and api_key and model)
        if model:
            self.model_cfg_btn.setText(f"⚙  {model}")
            self.model_cfg_btn.setToolTip(f"模型: {model}\nURL: {api_url[:40]}...\n点击修改配置")
        else:
            self.model_cfg_btn.setText("⚙  点击配置模型...")
            self.model_cfg_btn.setToolTip("点击配置 API URL、Key 和模型")
        if configured:
            self.cfg_status_dot.setStyleSheet("color:#22c55e; font-size:16px;")
            self.cfg_status_dot.setToolTip("已配置 ✓")
        else:
            self.cfg_status_dot.setStyleSheet("color:#94a3b8; font-size:16px;")
            self.cfg_status_dot.setToolTip("未配置 - 点击配置")

    def _on_send_all_toggled(self, checked: bool):
        """一次性发送开关：勾选后禁用批大小调节"""
        self.batch_size.setEnabled(not checked)
        if checked:
            self.batch_size.setStyleSheet("color:#94a3b8;")
        else:
            self.batch_size.setStyleSheet("")

    def get_values(self) -> dict:
        self._save_current_preset()
        cur = self._get_current_preset()
        return {
            "model_dir": self.model_dir.text().strip(),
            "language": self.lang.currentText(),
            "device": self.device.currentText(),
            "compute_type": self.precision.currentText(),
            "extract_audio": self.extract_cb.isChecked(),
            "vad_filter": self.vad_cb.isChecked(),
            "default_video_dir": self.default_dir.text().strip(),
            "reuse_auto_lang": self.reuse_lang_cb.isChecked(),
            "target_lang": self.target_lang.currentText(),
            "translation_model": cur["model"],
            "api_url": cur["api_url"],
            "api_key": cur["api_key"],
            "pipeline": self.pipeline_cb.isChecked(),
            "translation_only": self.only_zh_cb.isChecked(),
            "translation_batch_size": self.batch_size.value(),
            "send_all": self.send_all_cb.isChecked(),
            "pause_before_embed": self.pause_embed_cb.isChecked(),
            "backup_max_files": self.backup_max.value(),
            "presets": self._presets,
            "active_preset": self._active_id,
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

