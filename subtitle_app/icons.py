"""统一线性图标；使用 Qt 绘制并跟随控件的主题、禁用状态和显示缩放。"""
import weakref

from PySide6.QtCore import QByteArray, QBuffer, QIODevice, QRectF, QSize, Qt
from PySide6.QtGui import QIcon, QIconEngine, QPainter, QPalette, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QApplication, QPushButton


_SHAPES = {
    "folder": '<path d="M3 7V5h6l2 2h10v13H3Z"/>',
    "file": '<path d="M14 3H5v18h14V8Zm0 0v5h5M8 13h8M8 17h6"/>',
    "video": '<rect x="3" y="5" width="18" height="14" rx="2"/><path d="m10 9 5 3-5 3Z"/>',
    "audio": '<path d="M9 17V5l10-2v12M9 8l10-2"/><ellipse cx="6" cy="18" rx="3" ry="3"/><ellipse cx="16" cy="16" rx="3" ry="3"/>',
    "pin": '<path d="m8 3 8 0-1 6 3 4H6l3-4ZM12 13v8"/>',
    "settings": '<path d="M4 6h16M4 12h16M4 18h16"/><path d="M8 3v6M16 9v6M10 15v6"/>',
    "play": '<path d="m8 4 12 8-12 8Z"/>',
    "stop": '<rect x="5" y="5" width="14" height="14" rx="2"/>',
    "retry": '<path d="M20 10a8 8 0 1 0-2 8M20 4v6h-6"/>',
    "model": '<rect x="6" y="6" width="12" height="12" rx="2"/><rect x="9" y="9" width="6" height="6" rx="1"/><path d="M9 3v3M15 3v3M9 18v3M15 18v3M3 9h3M3 15h3M18 9h3M18 15h3"/>',
    "embed": '<path d="M4 4h16v16H4ZM8 4v16M16 4v16M4 8h4M4 16h4M16 8h4M16 16h4M10 12h4m-2-2 2 2-2 2"/>',
    "extract": '<path d="M4 9V4h16v5M12 3v12m-4-4 4 4 4-4M4 15v5h16v-5"/>',
    "export": '<path d="M12 16V3m-4 4 4-4 4 4M4 14v6h16v-6"/>',
    "edit": '<path d="m15 4 5 5M4 20l1-6L16 3l5 5-11 11Z"/>',
    "save": '<path d="M4 3h13l3 3v15H4ZM8 3v6h8V3M8 21v-8h8v8"/>',
    "search": '<circle cx="10" cy="10" r="6"/><path d="m15 15 6 6"/>',
    "time": '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l4 2"/>',
    "trash": '<path d="M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7M14 10v7"/>',
    "close": '<path d="m6 6 12 12M18 6 6 18"/>',
    "check": '<path d="m4 12 5 5L20 6"/>',
    "select": '<rect x="3" y="3" width="18" height="18" rx="2"/><path d="m7 12 3 3 7-7"/>',
    "plus": '<path d="M12 4v16M4 12h16"/>',
    "copy": '<rect x="8" y="8" width="13" height="13" rx="2"/><path d="M16 8V3H3v13h5"/>',
    "history": '<path d="M3 10a9 9 0 1 1 2 8M3 4v6h6M12 7v5l4 2"/>',
    "restore": '<path d="m8 4-5 5 5 5M3 9h10a7 7 0 0 1 0 14"/>',
    "skip": '<path d="m4 5 10 7-10 7ZM19 5v14"/>',
    "chevron-left": '<path d="m15 5-7 7 7 7"/>',
    "chevron-right": '<path d="m9 5 7 7-7 7"/>',
    "chevron-down": '<path d="m5 9 7 7 7-7"/>',
    "sun": '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M2 12h2M20 12h2M5 5l1.5 1.5M17.5 17.5 19 19M5 19l1.5-1.5M17.5 6.5 19 5"/>',
    "moon": '<path d="M20 15A9 9 0 0 1 9 4a9 9 0 1 0 11 11Z"/>',
}

_PREFIX_ICONS = {
    "📂": "folder", "📁": "folder", "📌": "pin", "⚙": "settings",
    "▶": "play", "⏹": "stop", "🔄": "retry", "🚀": "model",
    "📦": "embed", "📤": "export", "✏": "edit", "💾": "save",
    "🔍": "search", "⏱": "time", "🗑": "trash", "✕": "close",
    "☑": "select", "➕": "plus", "📋": "copy", "↩": "restore",
    "⏭": "skip", "✅": "check", "◀": "chevron-left",
}


class _LineIconEngine(QIconEngine):
    def __init__(self, name, widget=None, color=None):
        super().__init__()
        self.name = name
        self._widget = weakref.ref(widget) if widget is not None else lambda: None
        self._color = color
        self._renderers = {}

    def clone(self):
        return _LineIconEngine(self.name, self._widget(), self._color)

    def paint(self, painter, rect, mode, state):
        widget = self._widget()
        try:
            palette = widget.palette() if widget is not None else QApplication.palette()
        except RuntimeError:
            palette = QApplication.palette()
        group = QPalette.Disabled if mode == QIcon.Disabled else QPalette.Active
        role = QPalette.HighlightedText if mode == QIcon.Selected else QPalette.ButtonText
        color = self._color or palette.color(group, role).name()
        if color not in self._renderers:
            svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" '
                   f'fill="none" stroke="{color}" stroke-width="1.8" '
                   f'stroke-linecap="round" stroke-linejoin="round">'
                   f'{_SHAPES[self.name]}</svg>')
            self._renderers[color] = QSvgRenderer(QByteArray(svg.encode("utf-8")))
        # 保持正方形比例，避免被窄按钮拉伸。
        side = min(rect.width(), rect.height())
        target = QRectF(rect.x() + (rect.width() - side) / 2,
                        rect.y() + (rect.height() - side) / 2, side, side)
        self._renderers[color].render(painter, target)

    def pixmap(self, size, mode, state):
        pixmap = QPixmap(size)
        pixmap.fill(Qt.transparent)
        painter = QPainter(pixmap)
        self.paint(painter, pixmap.rect(), mode, state)
        painter.end()
        return pixmap


def make_icon(name, widget=None, color=None):
    return QIcon(_LineIconEngine(name, widget, color))


def set_button_icon(button, name):
    button.setIcon(make_icon(name, button))


def icon_data_url(name, color, size=32):
    """为现有富文本占位图提供同一套图标，不增加额外控件。"""
    buffer = QBuffer()
    buffer.open(QIODevice.WriteOnly)
    make_icon(name, color=color).pixmap(size, size).save(buffer, "PNG")
    return "data:image/png;base64," + bytes(buffer.data().toBase64()).decode("ascii")


def action_button(text="", parent=None, *, icon=None, accessible_name=None):
    """保留按钮文案与信号行为，将已有装饰字符替换成可缩放图标。"""
    for prefix, name in _PREFIX_ICONS.items():
        if text.startswith(prefix):
            text = text[len(prefix):].lstrip("\ufe0f ")
            icon = icon or name
            break
    if icon == "export" and text.startswith("提取"):
        icon = "extract"
    if icon == "copy" and text.startswith("处理历史"):
        icon = "history"
    button = QPushButton(text, parent)
    if icon:
        button.setIconSize(QSize(16, 16))
        set_button_icon(button, icon)
    if accessible_name:
        button.setAccessibleName(accessible_name)
    return button
