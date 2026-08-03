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


def load_theme_colors():
    """从 config.json 读取浅色/深色配色字典，返回 (light, dark)。"""
    light = {k: v for k, v in cfg.theme.light.__dict__.items()}
    dark = {k: v for k, v in cfg.theme.dark.__dict__.items()}
    return light, dark


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
    color = QColor("#fbbf24")  # 琥珀黄，深色 header 上醒目
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


def build_qss(colors: dict, is_dark: bool) -> str:
    """根据配色与明暗模式生成全局 QSS 样式表"""
    c = colors
    check_png = checkmark_png()
    border_radius = "border-radius:8px;"
    alt_bg = "#1c1f33" if is_dark else "#f8fafc"
    sel_bg = "#2f3554" if is_dark else "#e0e7ff"
    hover_bg = "#232741" if is_dark else "#eef2ff"
    theme_key = "dark" if is_dark else "light"
    arrow = _arrow_png(theme_key, c['text_muted'])
    return f"""
        QMainWindow {{ background: {c['bg']}; }}
        QWidget {{ background: {c['bg']}; color: {c['text']}; font-size: 13px; }}
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
        QFrame#header {{
            background: qlineargradient(x1:0,y1:0,x2:1,y2:0,
                stop:0 {c['header']}, stop:0.62 {c['header']}, stop:1 {c['accent']});
            border: none;
            border-bottom: 1px solid {c['border']};
        }}
        QFrame#card {{ background: {c['card']}; {border_radius} border:1px solid {c['border']}; }}
        QFrame#filePanel, QFrame#previewPanel, QFrame#logPanel {{
            background: {c['card']}; {border_radius}
            border:1px solid {c['border']};
        }}
        QFrame#progressPanel {{ background:{c['card']}; {border_radius} border:1px solid {c['border']}; }}
        QGroupBox {{
            background: {c['card']}; {border_radius}
            border:1px solid {c['border']};
            margin-top:8px; padding:8px 8px 8px 8px;
            font-weight:600; color:{c['accent']};
        }}
        QGroupBox::title {{
            subcontrol-origin:margin; left:12px; padding:0 7px;
            background:{c['card']};
        }}
        QLineEdit, QComboBox, QTextEdit, QListWidget {{
            background:{c['card']}; color:{c['text']};
            border:1px solid {c['border']}; {border_radius} padding:7px 9px;
            selection-background-color:{c['accent']}; selection-color:white;
        }}
        QLineEdit:focus, QComboBox:focus, QTextEdit:focus, QListWidget:focus {{
            border:1px solid {c['accent']};
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
            padding:7px 14px; font-weight:500;
        }}
        QPushButton:hover {{ background:{c['border']}; border-color:{c['accent']}; }}
        QPushButton:pressed {{ padding-top:8px; padding-bottom:6px; }}
        QPushButton:disabled {{ color:{c['text_muted']}; border-color:{c['border']}; background:{c['bg']}; }}
        QPushButton#bottomBtn {{ padding:9px 15px; font-size:13px; font-weight:600; }}
        QPushButton#startBtn {{ background:{c['success']}; color:white; border:none; font-weight:bold; padding:10px 22px; font-size:13px; }}
        QPushButton#startBtn:hover {{ background:#16a34a; }}
        QPushButton#startBtn:disabled {{ background:{c['text_muted']}; }}
        QPushButton#stopBtn {{ background:{c['danger']}; color:white; border:none; font-weight:bold; padding:10px 22px; font-size:13px; }}
        QPushButton#stopBtn:hover {{ background:#dc2626; }}
        QPushButton#stopBtn:disabled {{ background:{c['text_muted']}; }}
        QPushButton#accentBtn {{ background:{c['accent']}; color:white; border:none; padding:8px 16px; font-weight:600; }}
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
            padding:9px 18px; margin-right:3px;
            border:1px solid transparent; border-bottom:none;
            border-top-left-radius:8px; border-top-right-radius:8px;
        }}
        QTabBar::tab:hover {{ color:{c['accent']}; }}
        QTabBar::tab:selected {{ background:{c['card']}; color:{c['accent']}; border-color:{c['border']}; font-weight:600; }}
        QCheckBox {{ spacing:7px; font-weight:600; color:{c['text_sec']}; }}
        QCheckBox::indicator {{
            width:16px; height:16px;
            background:{c['card']}; border:1px solid {c['text_muted']};
            border-radius:3px;
        }}
        QCheckBox::indicator:hover {{ border-color:{c['accent']}; }}
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
        QSplitter::handle:horizontal {{ width:2px; }}
        QSplitter::handle:vertical {{ height:4px; }}
        QLabel {{ background:transparent; }}
        QListWidget#logList {{ background:{c['card']}; border:none; }}
        QListWidget#logList::item {{ padding:2px 4px; border-bottom:1px solid {c['border']}; }}
        QListWidget::item:hover {{ background:{c['bg']}; }}
        QListWidget::item:selected {{ background:{c['accent']}; color:white; }}
    """
