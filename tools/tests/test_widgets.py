#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""widgets.py 单元测试（仅测试非 Qt 依赖部分）"""
import unittest


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
