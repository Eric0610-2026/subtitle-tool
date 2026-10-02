#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
主题模块：配色加载、图标绘制、QSS 生成、系统深色模式检测。

从 qt_app.py 抽取，保持行为完全一致，仅移动代码位置。
"""
import logging
import math
import tempfile
from pathlib import Path

from PySide6.QtCore import Qt, QRectF, QPoint
from PySide6.QtGui import (
    QPixmap, QPainter, QPen, QPolygon, QPainterPath, QIcon, QColor,
)

from .config import cfg

logger = logging.getLogger(__name__)


_DEFAULT_COLORS = {
    "light": dict(bg="#f4f5f7", card="#ffffff", header="#eef2ff", accent="#6366f1",
                  text="#0f172a", text_sec="#475569", text_muted="#94a3b8",
                  border="#e2e8f0", success="#22c55e", danger="#ef4444"),
    "dark": dict(bg="#0b0d14", card="#16182a", header="#06070c", accent="#818cf8",
                 text="#e2e8f0", text_sec="#94a3b8", text_muted="#64748b",
                 border="#2a2b45", success="#22c55e", danger="#ef4444"),
}


def _normalize_colors(values, is_dark):
    defaults = _DEFAULT_COLORS["dark" if is_dark else "light"]
    values = values if isinstance(values, dict) else {}
    colors = {}
    for key, default in defaults.items():
        value = values.get(key, default)
        color = QColor(value.strip()) if isinstance(value, str) else QColor()
        if not color.isValid():
            logger.warning("主题颜色 %s=%r 无效，使用默认值 %s", key, value, default)
            color = QColor(default)
        colors[key] = color.name(QColor.NameFormat.HexRgb if color.alpha() == 255 else QColor.NameFormat.HexArgb)
    # 兼容旧配置：浅色主题以前也使用深色头部默认值。
    if not is_dark and colors["header"] == "#1a1a2e":
        colors["header"] = defaults["header"]
    return colors


def load_theme_colors():
    """从 config.json 读取浅色/深色配色字典，返回 (light, dark)。"""
    configured = getattr(cfg, "theme", None)
    colors = []
    for name, is_dark in (("light", False), ("dark", True)):
        value = getattr(configured, name, None)
        colors.append(_normalize_colors(vars(value) if hasattr(value, "__dict__") else value, is_dark))
    return colors[0], colors[1]


_CHECK_PNG_CACHE = None
_ARROW_PNG_CACHE: dict = {}


def _arrow_png(key: str, color_hex: str) -> str:
    """生成下拉箭头 PNG（按主题/状态缓存），返回用于 QSS url() 的绝对路径"""
    cached = _ARROW_PNG_CACHE.get(key)
    if cached:
        return cached
    path = Path(tempfile.gettempdir()) / f"zimu_arrow_{key}.png"
    if not path.exists():
        pm = QPixmap(12, 12)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(color_hex))
        tri = QPainterPath()
        tri.moveTo(2.0, 4.0)
        tri.lineTo(6.0, 9.0)
        tri.lineTo(10.0, 4.0)
        tri.closeSubpath()
        p.drawPath(tri)
        p.end()
        pm.save(str(path))
    _ARROW_PNG_CACHE[key] = path.as_posix()
    return _ARROW_PNG_CACHE[key]


def _spin_arrow_png(key: str, color_hex: str, direction: str) -> str:
    """生成微调框（QSpinBox/QDoubleSpinBox）上下箭头 PNG。

    QSS 里用 border 画三角形的技巧在 Qt 中不生效（渲染成方块），
    与下拉框一致走 PNG 方案。direction: "up" / "down"。
    """
    name = f"zimu_spin_{direction}_{key}"
    cached = _ARROW_PNG_CACHE.get(name)
    if cached:
        return cached
    path = Path(tempfile.gettempdir()) / f"{name}.png"
    if not path.exists():
        pm = QPixmap(10, 8)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(color_hex))
        tri = QPainterPath()
        if direction == "up":
            tri.moveTo(1.0, 6.5)
            tri.lineTo(5.0, 1.5)
            tri.lineTo(9.0, 6.5)
        else:
            tri.moveTo(1.0, 1.5)
            tri.lineTo(9.0, 1.5)
            tri.lineTo(5.0, 6.5)
        tri.closeSubpath()
        p.drawPath(tri)
        p.end()
        pm.save(str(path))
    _ARROW_PNG_CACHE[name] = path.as_posix()
    return _ARROW_PNG_CACHE[name]


def checkmark_png() -> str:
    """生成白色勾选标记 PNG（仅一次），返回用于 QSS url() 的绝对路径"""
    global _CHECK_PNG_CACHE
    if _CHECK_PNG_CACHE:
        return _CHECK_PNG_CACHE
    path = Path(tempfile.gettempdir()) / "zimu_checkmark.png"
    if not path.exists():
        pm = QPixmap(16, 16)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(QColor("white"), 2.6)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen)
        p.drawPolyline(QPolygon([QPoint(3, 9), QPoint(7, 13), QPoint(13, 4)]))
        p.end()
        pm.save(str(path))
    _CHECK_PNG_CACHE = path.as_posix()
    return _CHECK_PNG_CACHE


def make_sun_icon(size: int = 20) -> QIcon:
    """绘制太阳图标（浅色模式指示），避免 emoji 渲染不清"""
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    color = QColor("#b45309")  # 浅色头部使用深琥珀色，保证图标对比度
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(color)
    c = size / 2
    p.drawEllipse(QRectF(c - 3, c - 3, 6, 6))  # 中心圆
    pen = QPen(color, 1.6)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    p.setPen(pen)
    for i in range(8):
        ang = i * math.pi / 4
        p.drawLine(c + 4.6 * math.cos(ang), c + 4.6 * math.sin(ang),
                   c + 6.9 * math.cos(ang), c + 6.9 * math.sin(ang))
    p.end()
    return QIcon(pm)


def make_moon_icon(size: int = 20) -> QIcon:
    """绘制月亮图标（深色模式指示），避免 emoji 渲染不清"""
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    color = QColor("#fbbf24")
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(color)
    body = QPainterPath()
    body.addEllipse(QRectF(2.5, 2.5, 12, 12))
    hole = QPainterPath()
    hole.addEllipse(QRectF(7.5, 0.5, 12, 12))
    p.drawPath(body.subtracted(hole))  # 月牙
    p.end()
    return QIcon(pm)


def detect_system_dark() -> bool:
    """读取 Windows 系统主题设置，返回是否为深色模式"""
    try:
        import winreg
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                           r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize")
        v, _ = winreg.QueryValueEx(k, "AppsUseLightTheme")
        winreg.CloseKey(k)
        return v == 0
    except (OSError, TypeError) as e:
        logger.debug("读取系统主题失败: %s", e)
        return False


def _mix_hex(c1: str, c2: str, t: float) -> str:
    """按 Qt 颜色格式混合 RGB 分量（用于渐变中间色）。"""
    first, second = QColor(c1), QColor(c2)
    r1, g1, b1 = first.red(), first.green(), first.blue()
    r2, g2, b2 = second.red(), second.green(), second.blue()
    r, g, b = (round(a + (b_ - a) * t) for a, b_ in ((r1, r2), (g1, g2), (b1, b2)))
    return f"#{r:02x}{g:02x}{b:02x}"


def build_qss(colors: dict, is_dark: bool) -> str:
    """根据配色与明暗模式生成全局 QSS 样式表"""
    c = _normalize_colors(colors, is_dark)
    check_png = checkmark_png()
    border_radius = "border-radius:10px;"
    panel_radius = "border-radius:14px;"
    alt_bg = "#1c1f33" if is_dark else "#f8fafc"
    sel_bg = "#353c62" if is_dark else "#e0e7ff"
    hover_bg = "#272c49" if is_dark else "#eef2ff"
    surface_alt = "#20243a" if is_dark else "#f8fafc"
    theme_key = "dark" if is_dark else "light"
    arrow = _arrow_png(theme_key, c['text_muted'])
    # 浅色头部的整个渐变保持浅色，文字和徽标同步使用对应主题颜色。
    header_end = c['accent'] if is_dark else _mix_hex(c['header'], c['accent'], 0.12)
    header_mid = _mix_hex(c['header'], header_end, 0.45)
    header_text = "white" if is_dark else c['text']
    header_border = "rgba(255,255,255,0.12)" if is_dark else c['border']
    badge_text = "rgba(255,255,255,0.78)" if is_dark else c['accent']
    badge_bg = "rgba(255,255,255,0.12)" if is_dark else _mix_hex(c['header'], c['accent'], 0.08)
    badge_border = "rgba(255,255,255,0.16)" if is_dark else _mix_hex(c['header'], c['accent'], 0.18)
    header_meta = "rgba(255,255,255,0.68)" if is_dark else c['text_sec']
    spin_up = _spin_arrow_png(theme_key, c['text_sec'], "up")
    spin_down = _spin_arrow_png(theme_key, c['text_sec'], "down")
    return f"""
        QMainWindow {{ background: {c['bg']}; }}
        QWidget {{ background: {c['bg']}; color: {c['text']}; font-size: 13px; }}
        QFrame#header {{
            background: qlineargradient(x1:0,y1:0,x2:1,y2:0,
                stop:0 {c['header']}, stop:0.55 {c['header']},
                stop:0.85 {header_mid}, stop:1 {header_end});
            border: none;
            border-bottom: 1px solid {header_border};
        }}
        QLabel#appTitle {{ color:{header_text}; font-size:17px; font-weight:700; letter-spacing:0.2px; }}
        QLabel#headerBadge {{
            color:{badge_text}; background:{badge_bg};
            border:1px solid {badge_border}; border-radius:8px;
            min-height:15px; max-height:15px; padding:2px 7px;
            font-size:9px; font-weight:700; letter-spacing:0.7px;
        }}
        QLabel#headerMeta {{ color:{header_meta}; font-size:11px; }}
        QToolTip {{
            background: {c['card']}; color: {c['text']};
            border: 1px solid {c['accent']}; border-radius: 4px;
            padding: 5px 8px; font-size: 12px;
        }}
        QMenu {{
            background: {c['card']}; color: {c['text']};
            border: 1px solid {c['border']}; border-radius: 6px; padding: 4px;
        }}
        QMenu::item {{ padding: 6px 26px 6px 14px; border-radius: 4px; background: transparent; }}
        QMenu::item:selected {{ background: {sel_bg}; color: {c['text']}; }}
        QMenu::item:disabled {{ color: {c['text_muted']}; }}
        QMenu::separator {{ height: 1px; background: {c['border']}; margin: 4px 8px; }}
        QTableWidget#subtitlePreview {{
            background: transparent; color: {c['text']};
            border: none; gridline-color: transparent;
            alternate-background-color: {alt_bg};
            selection-background-color: {sel_bg}; selection-color: {c['text']};
            outline: 0;
        }}
        QTableWidget#subtitlePreview::item:hover {{ background: {hover_bg}; }}
        QTableWidget#subtitlePreview::item:selected {{ background: {sel_bg}; color: {c['text']}; }}
        QHeaderView {{ background: transparent; border: none; }}
        QHeaderView::section {{
            background: {c['bg']}; color: {c['text_sec']};
            border: none; border-bottom: 1px solid {c['border']};
            padding: 5px 8px; font-size: 11px; font-weight: 600;
        }}
        QTableCornerButton::section {{ background: transparent; border: none; }}
        QFrame#card {{ background: {c['card']}; {panel_radius} border:1px solid {c['border']}; }}
        QFrame#filePanel, QFrame#previewPanel, QFrame#logPanel {{
            background: {c['card']}; {panel_radius}
            border:1px solid {c['border']};
        }}
        QFrame#progressPanel {{ background:{c['card']}; {panel_radius} border:1px solid {c['border']}; }}
        QFrame#workspaceBar {{
            background:{surface_alt}; {panel_radius} border:1px solid {c['border']};
        }}
        QFrame#actionBar {{
            background:{c['card']}; border-top:1px solid {c['border']};
        }}
        QLabel#panelTitle {{
            color:{c['text']}; font-size:13px; font-weight:700; padding:2px 0;
        }}
        QGroupBox {{
            background: {c['card']}; {border_radius}
            border:1px solid {c['border']};
            margin-top:10px; padding:10px 10px 8px 10px;
            font-weight:600; color:{c['accent']};
        }}
        QGroupBox::title {{
            subcontrol-origin:margin; left:12px; padding:0 7px;
            background:{c['card']};
        }}
        QLineEdit, QComboBox, QTextEdit, QListWidget, QAbstractSpinBox {{
            background:{c['card']}; color:{c['text']};
            border:1px solid {c['border']}; {border_radius} padding:8px 10px;
            selection-background-color:{c['accent']}; selection-color:white;
        }}
        QLineEdit:focus, QComboBox:focus, QTextEdit:focus, QListWidget:focus,
        QAbstractSpinBox:focus {{
            border:1px solid {c['accent']};
        }}
        QLineEdit:disabled, QComboBox:disabled, QAbstractSpinBox:disabled {{
            color:{c['text_muted']}; border-color:{c['border']}; background:{c['bg']};
        }}
        QAbstractSpinBox::up-button, QAbstractSpinBox::down-button {{
            width:16px; border:none; background:transparent;
        }}
        QAbstractSpinBox::up-button:hover, QAbstractSpinBox::down-button:hover,
        QAbstractSpinBox::up-button:pressed, QAbstractSpinBox::down-button:pressed {{
            background:{hover_bg}; border-radius:3px;
        }}
        QAbstractSpinBox::up-arrow {{
            image: url("{spin_up}"); width:10px; height:8px;
        }}
        QAbstractSpinBox::down-arrow {{
            image: url("{spin_down}"); width:10px; height:8px;
        }}
        QComboBox:hover {{ border-color:{c['accent']}; }}
        QComboBox:disabled {{
            color:{c['text_muted']}; border-color:{c['border']}; background:{c['bg']};
        }}
        QComboBox::drop-down {{
            border:none; width:26px; subcontrol-origin:padding;
            subcontrol-position:center right; background:{c['card']};
        }}
        QComboBox::down-arrow {{
            image: url("{arrow}"); width:12px; height:12px;
            subcontrol-origin:padding; subcontrol-position:center;
        }}
        QComboBox QAbstractItemView {{
            background:{c['card']}; color:{c['text']};
            border:1px solid {c['border']}; {border_radius}
            selection-background-color:{c['accent']}; selection-color:white;
            outline:0; padding:4px;
        }}
        QComboBox QFrame {{
            /* 下拉弹层容器：默认 Fusion 灰底会从圆角/边缘渗出，统一为卡片色 */
            background:{c['card']}; border:none;
        }}
        QComboBox QAbstractItemView::item {{
            min-height:24px; padding:4px 10px;
        }}
        QComboBox QAbstractItemView::item:hover {{
            background:{hover_bg}; color:{c['text']};
        }}
        QComboBox QAbstractItemView::item:selected {{ background:{c['accent']}; color:white; }}
        QPushButton {{
            background:{c['card']}; color:{c['text']};
            border:1px solid {c['border']}; {border_radius}
            padding:8px 14px; font-weight:600;
        }}
        QPushButton:hover {{ background:{c['border']}; border-color:{c['accent']}; }}
        QPushButton:pressed {{ padding-top:8px; padding-bottom:6px; }}
        QPushButton:disabled {{ color:{c['text_muted']}; border-color:{c['border']}; background:{c['bg']}; }}
        QPushButton:focus {{ border-color:{c['accent']}; }}
        QPushButton#bottomBtn {{ padding:9px 15px; font-size:13px; font-weight:600; }}
        QPushButton#startBtn {{ background:{c['success']}; color:white; border:none; border-radius:10px; font-weight:bold; padding:11px 24px; font-size:13px; }}
        QPushButton#startBtn:hover {{ background:#16a34a; }}
        QPushButton#startBtn:disabled {{ background:{c['text_muted']}; }}
        QPushButton#stopBtn {{ background:{c['danger']}; color:white; border:none; border-radius:10px; font-weight:bold; padding:11px 24px; font-size:13px; }}
        QPushButton#stopBtn:hover {{ background:#dc2626; }}
        QPushButton#stopBtn:disabled {{ background:{c['text_muted']}; }}
        QPushButton#accentBtn {{ background:{c['accent']}; color:white; border:none; border-radius:10px; padding:9px 16px; font-weight:700; }}
        QPushButton#accentBtn:hover {{ background:#4f46e5; }}
        QPushButton#actionBtn {{ padding:6px 11px; font-size:12px; }}
        QProgressBar {{
            background:{c['border']}; border:none; {border_radius}
            color:{c['text']}; text-align:center; font-size:11px; font-weight:600;
        }}
        QProgressBar::chunk {{
            background:qlineargradient(x1:0,y1:0,x2:1,y2:0,
                stop:0 {c['accent']}, stop:1 #a78bfa); {border_radius}
        }}
        QTabWidget::pane {{ background:{c['card']}; border:none; }}
        QTabBar::tab {{
            background:{c['bg']}; color:{c['text_sec']};
            padding:10px 18px; margin-right:4px;
            border:1px solid transparent; border-bottom:none;
            border-top-left-radius:8px; border-top-right-radius:8px;
        }}
        QTabBar::tab:hover {{ color:{c['accent']}; }}
        QTabBar::tab:selected {{ background:{c['card']}; color:{c['accent']}; border-color:{c['border']}; font-weight:700; }}
        QCheckBox {{ spacing:7px; font-weight:600; color:{c['text_sec']}; background:transparent; }}
        QCheckBox::indicator {{
            width:16px; height:16px;
            background:{c['card']}; border:1px solid {c['text_muted']};
            border-radius:3px;
        }}
        QCheckBox::indicator:hover {{ border-color:{c['accent']}; }}
        QCheckBox::indicator:disabled {{
            background:{c['bg']}; border:1px solid {c['text_muted']};
        }}
        QCheckBox::indicator:checked {{
            background:{c['accent']}; border-color:{c['accent']};
            image: url("{check_png}");
        }}
        QScrollBar:vertical {{ width:9px; background:{c['bg']}; border:none; margin:2px; }}
        QScrollBar::handle:vertical {{ background:{c['border']}; {border_radius} min-height:28px; }}
        QScrollBar::handle:vertical:hover {{ background:{c['text_muted']}; }}
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height:0; border:none; }}
        QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background:none; }}
        QScrollBar:horizontal {{ height:9px; background:{c['bg']}; border:none; margin:2px; }}
        QScrollBar::handle:horizontal {{ background:{c['border']}; {border_radius} min-width:28px; }}
        QScrollBar::handle:horizontal:hover {{ background:{c['text_muted']}; }}
        QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width:0; border:none; }}
        QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {{ background:none; }}
        QSplitter::handle {{ background:{c['border']}; }}
        QSplitter::handle:hover {{ background:{c['accent']}; }}
        QSplitter::handle:horizontal {{ width:5px; }}
        QSplitter::handle:vertical {{ height:5px; }}
        QLabel {{ background:transparent; }}
        QListWidget#logList {{ background:{c['card']}; border:none; }}
        QListWidget#logList::item {{ padding:0; border-bottom:1px solid {c['border']}; }}
        QListWidget::item:hover {{ background:{c['bg']}; }}
        QListWidget::item:selected {{ background:{c['accent']}; color:white; }}
        QListWidget[dragOver="true"] {{
            border:1px dashed {c['accent']}; background:{hover_bg};
        }}
    """
