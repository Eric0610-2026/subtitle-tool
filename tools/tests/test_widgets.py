#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""控件与面板测试：预览、日志和按钮交互。"""
import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QMessageBox, QPushButton, QRadioButton,
    QToolButton, QVBoxLayout, QWidget,
)

from subtitle_app.widgets import install_button_cursors


class TestPreviewIncrementalRendering(unittest.TestCase):
    @staticmethod
    def _make_panel():
        import os
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication
        from subtitle_app.panels import PreviewPanel

        app = QApplication.instance() or QApplication([])
        return app, PreviewPanel()

    def test_render_reuses_cells_and_restores_signal_and_update_states(self):
        app, panel = self._make_panel()
        first = "1\n00:00:01,000 --> 00:00:02,000\nfirst"
        second = "2\n00:00:02,000 --> 00:00:03,000\nsecond"
        panel.set_text(first)
        first_item = panel.preview.item(0, 2)
        changed = []
        panel.preview.itemChanged.connect(changed.append)
        panel.append(second)
        with patch.object(panel, "_palette_colors", wraps=panel._palette_colors) as colors:
            panel._flush_live_render()
        self.assertEqual(colors.call_count, 1)
        self.assertIs(panel.preview.item(0, 2), first_item)
        self.assertEqual(changed, [])
        self.assertFalse(panel.preview.signalsBlocked())
        self.assertTrue(panel.preview.updatesEnabled())

        panel.preview.blockSignals(True)
        panel.preview.setUpdatesEnabled(False)
        panel._render_structured_preview()
        self.assertTrue(panel.preview.signalsBlocked())
        self.assertFalse(panel.preview.updatesEnabled())
        panel.preview.blockSignals(False)
        panel.preview.setUpdatesEnabled(True)
        panel.clear()
        self.assertEqual(panel.preview.rowCount(), 0)

    def test_theme_keeps_highlight_without_rewriting_source_text(self):
        app, panel = self._make_panel()
        from PySide6.QtCore import Qt
        text = "1\n00:00:01,000 --> 00:00:02,000\n  original  \n  translation  \n"
        panel.set_text(text)
        panel.setReadOnly(False)
        panel.highlight_rows([0])
        self.assertEqual(panel.get_text(), text)
        panel.refresh_theme()
        self.assertEqual(panel._highlighted_rows, {0})
        self.assertEqual(panel.preview.item(0, 2).background().color().name(), "#fde68a")
        panel.clear_highlight()
        self.assertEqual(panel.get_text(), text)
        self.assertEqual(panel._highlighted_rows, set())
        self.assertEqual(panel.preview.item(0, 2).background().style(), Qt.NoBrush)
        panel.highlight_rows([0])
        panel.set_text(text)
        self.assertEqual(panel._highlighted_rows, set())
        self.assertEqual(panel.preview.item(0, 2).background().style(), Qt.NoBrush)

    def test_frozen_cells_edit_the_first_block_without_losing_hidden_tail(self):
        app, panel = self._make_panel()
        from subtitle_app.panels import MAX_LIVE_PREVIEW_BLOCKS
        blocks = [f"{i + 1}\n00:00:01,000 --> 00:00:02,000\nsegment_{i}"
                  for i in range(MAX_LIVE_PREVIEW_BLOCKS)]
        panel.set_text("\n\n".join(blocks))
        first_item = panel.preview.item(0, 2)
        panel.append(f"{MAX_LIVE_PREVIEW_BLOCKS + 1}\n00:00:02,000 --> 00:00:03,000\nlast")
        panel._flush_live_render()
        self.assertIs(panel.preview.item(0, 2), first_item)
        self.assertEqual(first_item.text(), "segment_0")
        panel.setReadOnly(False)
        first_item.setText("changed")
        updated = panel.get_text().split("\n\n")
        self.assertTrue(updated[0].endswith("changed"))
        self.assertEqual(updated[1], blocks[1])
        self.assertTrue(updated[-1].endswith("last"))

    @staticmethod
    def _blocks(count, translated=False):
        return [f"{i + 1}\n00:00:01,000 --> 00:00:02,000\nsegment_{i}"
                + (f"\ntranslation_{i}" if translated else "") for i in range(count)]

    def test_live_preview_stops_rendering_after_first_100_rows(self):
        app, panel = self._make_panel()
        blocks = self._blocks(5000)
        panel.append("\n\n".join(blocks[:99]))
        panel._flush_live_render()
        self.assertEqual(panel.preview.rowCount(), 99)
        panel.append(blocks[99])
        panel._flush_live_render()
        self.assertEqual(panel.preview.rowCount(), 100)
        first = panel.preview.item(0, 2)
        with patch.object(panel, "_render_structured_preview", wraps=panel._render_structured_preview) as render:
            for block in blocks[100:]:
                panel.append(block)
            panel._flush_live_render()
            self.assertFalse(panel._render_timer.isActive())
            render.assert_not_called()
        self.assertIs(panel.preview.item(0, 2), first)
        self.assertEqual(first.text(), "segment_0")
        self.assertEqual(panel.preview.item(99, 2).text(), "segment_99")
        self.assertEqual(len(panel._preview_blocks), 5000)
        self.assertIn("segment_4999", panel.get_text())
        panel.clear()

    def test_final_translated_text_is_saved_without_changing_frozen_preview(self):
        app, panel = self._make_panel()
        panel.append("\n\n".join(self._blocks(100)))
        panel._flush_live_render()
        final = "\n\n".join(self._blocks(5000, translated=True))
        with patch.object(panel, "_render_structured_preview", wraps=panel._render_structured_preview) as render:
            panel.set_text(final, preserve_live_preview=True)
            render.assert_not_called()
        self.assertEqual(panel.get_text(), final)
        self.assertEqual(panel.preview.rowCount(), 100)
        self.assertEqual(panel.preview.item(0, 3).text(), "—")
        panel.setReadOnly(False)
        from PySide6.QtWidgets import QAbstractItemView
        self.assertEqual(panel.preview.editTriggers(), QAbstractItemView.NoEditTriggers)
        panel.refresh_theme()
        self.assertEqual(panel.preview.item(0, 3).text(), "—")
        self.assertEqual(panel.get_text(), final)
        # 用户主动打开完整字幕/提交编辑时，预览重新读取前 100 行。
        panel.set_text(final)
        panel.setReadOnly(False)
        self.assertEqual(panel.preview.item(0, 3).text(), "translation_0")
        self.assertNotEqual(panel.preview.editTriggers(), QAbstractItemView.NoEditTriggers)
        panel.clear()

    def test_clear_restarts_preview_for_the_next_file(self):
        app, panel = self._make_panel()
        panel.append("\n\n".join(self._blocks(100)))
        panel._flush_live_render()
        panel.clear()
        panel.append("1\n00:00:01,000 --> 00:00:02,000\nnew file")
        self.assertTrue(panel._render_timer.isActive())
        panel._flush_live_render()
        self.assertEqual(panel.preview.rowCount(), 1)
        self.assertEqual(panel.preview.item(0, 2).text(), "new file")
        self.assertNotIn("segment_0", panel.get_text())
        panel.clear()

    def test_opening_large_srt_displays_first_100_and_preserves_full_text(self):
        app, panel = self._make_panel()
        full = "\n\n".join(self._blocks(5000, translated=True))
        panel.set_text(full)
        self.assertEqual(panel.preview.rowCount(), 100)
        self.assertEqual(panel.preview.item(0, 2).text(), "segment_0")
        self.assertEqual(panel.preview.item(99, 2).text(), "segment_99")
        self.assertEqual(panel.get_text(), full)
        panel.clear()

    def test_append_then_edit_keeps_full_text_and_source_mapping(self):
        import os
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication
        from subtitle_app.panels import PreviewPanel

        app = QApplication.instance() or QApplication([])
        panel = PreviewPanel()
        first = "1\n00:00:01,000 --> 00:00:02,000\nfirst"
        second = "2\n00:00:02,000 --> 00:00:03,000\nsecond"
        panel.set_text(first)
        panel.append(second)
        panel._flush_live_render()
        self.assertEqual(panel.preview.rowCount(), 2)
        self.assertEqual(panel.preview.item(1, 2).text(), "second")

        panel.setReadOnly(False)
        panel.preview.item(1, 2).setText("changed")
        self.assertIn("first", panel.get_text())
        self.assertIn("changed", panel.get_text())
        panel.refresh_theme()
        self.assertEqual(panel.preview.item(1, 2).text(), "changed")


class TestLogPanelNoSelection(unittest.TestCase):
    """日志列表不可选中：点击条目不残留高亮（回归）"""

    def test_log_list_selection_disabled(self):
        import os
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QApplication, QAbstractItemView
        app = QApplication.instance() or QApplication([])
        from subtitle_app.panels import LogPanel
        panel = LogPanel()
        panel.add_entry("hello")
        panel.add_entry("world")
        self.assertEqual(panel.log_list.selectionMode(), QAbstractItemView.NoSelection)
        self.assertEqual(panel.log_list.focusPolicy(), Qt.NoFocus)
        # 即便程序化设置当前行，也不会产生选中高亮
        panel.log_list.setCurrentRow(1)
        idx = panel.log_list.indexFromItem(panel.log_list.item(1))
        self.assertFalse(panel.log_list.selectionModel().isSelected(idx))
        # 导出功能不依赖选中态，仍能取到全部条目
        self.assertEqual(len(panel.get_all_lines()), 2)


class TestButtonCursors(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        install_button_cursors(cls.app)

    def test_hover_press_release_and_click_for_button_types(self):
        for button_type in (QPushButton, QToolButton, QCheckBox, QRadioButton):
            with self.subTest(button_type=button_type.__name__):
                button = button_type()
                button.setText("Click")
                self.addCleanup(button.close)
                clicks = []
                button.clicked.connect(lambda checked=False: clicks.append(checked))
                button.show()
                self.app.processEvents()
                QTest.mouseMove(button, button.rect().center())
                self.assertEqual(button.cursor().shape(), Qt.PointingHandCursor)
                QTest.mousePress(button, Qt.LeftButton, pos=button.rect().center())
                self.assertEqual(button.cursor().shape(), Qt.PointingHandCursor)
                QTest.mouseRelease(button, Qt.LeftButton, pos=button.rect().center())
                self.assertEqual(button.cursor().shape(), Qt.PointingHandCursor)
                self.assertEqual(len(clicks), 1)
                button.setEnabled(False)
                self.assertEqual(button.cursor().shape(), Qt.ArrowCursor)
                QTest.mouseClick(button, Qt.LeftButton, pos=button.rect().center())
                self.assertEqual(len(clicks), 1)
                button.setEnabled(True)
                self.assertEqual(button.cursor().shape(), Qt.PointingHandCursor)
                button.close()

    def test_parent_disable_and_unrelated_widgets(self):
        parent = QWidget()
        self.addCleanup(parent.close)
        layout = QVBoxLayout(parent)
        button = QPushButton("Click")
        layout.addWidget(button)
        parent.show()
        self.app.processEvents()
        self.assertEqual(parent.cursor().shape(), Qt.ArrowCursor)
        parent.setEnabled(False)
        self.assertEqual(button.cursor().shape(), Qt.ArrowCursor)
        parent.setEnabled(True)
        self.assertEqual(button.cursor().shape(), Qt.PointingHandCursor)

    def test_dynamic_message_box_and_style_change(self):
        box = QMessageBox()
        self.addCleanup(box.close)
        box.setStandardButtons(QMessageBox.Ok | QMessageBox.Cancel)
        box.show()
        self.app.processEvents()
        for color in ("white", "#202020"):
            box.setStyleSheet(f"QPushButton {{ background: {color}; }}")
            self.app.processEvents()
            for button in box.buttons():
                self.assertEqual(button.cursor().shape(), Qt.PointingHandCursor)

    def test_reinstall_preserves_filter_and_existing_buttons(self):
        button = QPushButton("Click")
        self.addCleanup(button.close)
        cursor_filter = self.app._button_cursor_filter
        install_button_cursors(self.app)
        self.assertIs(self.app._button_cursor_filter, cursor_filter)
        button.ensurePolished()
        self.assertEqual(button.cursor().shape(), Qt.PointingHandCursor)


if __name__ == "__main__":
    unittest.main()
