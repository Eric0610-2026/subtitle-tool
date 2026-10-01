#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""widgets.py 单元测试（仅测试非 Qt 依赖部分）"""
import unittest
from unittest.mock import patch


class TestVisibleBlockSlice(unittest.TestCase):
    """panels._visible_block_slice：预览渲染最多显示最近若干块（_raw_text 保留全文）"""

    def test_slice_keeps_newest_blocks_with_offset(self):
        from subtitle_app.panels import _visible_block_slice
        block = lambda i: f"{i}\n00:00:0{i},000 --> 00:00:0{i + 1},000\nseg_{i}"
        blocks = [block(i) for i in range(5)]
        # 未超上限不裁剪，偏移 0
        visible, offset = _visible_block_slice(blocks, max_blocks=10)
        self.assertEqual(visible, blocks)
        self.assertEqual(offset, 0)
        # 超过上限只渲染最近 max_blocks 块，偏移指向全文中的真实起点
        visible, offset = _visible_block_slice(blocks, max_blocks=2)
        self.assertEqual(visible, [block(3), block(4)])
        self.assertEqual(offset, 3)


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

    def test_reused_cells_edit_the_correct_block_after_preview_window_moves(self):
        app, panel = self._make_panel()
        from subtitle_app.panels import MAX_LIVE_PREVIEW_BLOCKS
        blocks = [f"{i + 1}\n00:00:01,000 --> 00:00:02,000\nsegment_{i}"
                  for i in range(MAX_LIVE_PREVIEW_BLOCKS)]
        panel.set_text("\n\n".join(blocks))
        first_item = panel.preview.item(0, 2)
        panel.append(f"{MAX_LIVE_PREVIEW_BLOCKS + 1}\n00:00:02,000 --> 00:00:03,000\nlast")
        panel._flush_live_render()
        self.assertIs(panel.preview.item(0, 2), first_item)
        self.assertEqual(first_item.text(), "segment_1")
        panel.setReadOnly(False)
        first_item.setText("changed")
        updated = panel.get_text().split("\n\n")
        self.assertEqual(updated[0], blocks[0])
        self.assertTrue(updated[1].endswith("changed"))
        self.assertTrue(updated[-1].endswith("last"))

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


if __name__ == "__main__":
    unittest.main()
