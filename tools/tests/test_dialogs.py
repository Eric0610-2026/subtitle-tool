#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""测试 dialogs 的纯函数：嵌入对话框字幕匹配（_find_matching_subtitle）"""
import tempfile
import unittest
import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget

from subtitle_app.dialogs import SettingsDialog, _find_matching_subtitle
from subtitle_app.theme import build_qss, load_theme_colors
from subtitle_app.widgets import PopupFade, _PopupSeparatorDelegate


class TestLanguagePopups(unittest.TestCase):
    def test_both_language_popups_are_rounded_separated_and_fade_in(self):
        app = QApplication.instance() or QApplication([])
        colors, _ = load_theme_colors()
        parent = QWidget()
        parent.colors = colors
        parent.setStyleSheet(build_qss(colors, False))
        dialog = SettingsDialog(parent, {})
        try:
            parent.show()
            dialog.show()
            app.processEvents()
            for combo in (dialog.lang, dialog.target_lang):
                original = combo.currentData()
                combo.showPopup()
                app.processEvents()
                popup = combo.view().window()
                self.assertFalse(popup.mask().contains(QPoint(0, 0)))
                self.assertIsInstance(combo.view().itemDelegate(), _PopupSeparatorDelegate)
                self.assertIsNotNone(combo.findChild(PopupFade))
                self.assertLess(popup.windowOpacity(), 1.0)
                QTest.qWait(230)
                self.assertEqual(popup.windowOpacity(), 1.0)
                self.assertEqual(combo.currentData(), original)
                combo.hidePopup()
        finally:
            dialog.close()
            parent.close()

    def test_popup_bottom_padding_uses_theme_background(self):
        app = QApplication.instance() or QApplication([])
        for is_dark, colors in enumerate(load_theme_colors()):
            parent = QWidget()
            parent.colors = colors
            parent.setStyleSheet(build_qss(colors, bool(is_dark)))
            dialog = SettingsDialog(parent, {})
            try:
                parent.show()
                dialog.show()
                app.processEvents()
                dialog.lang.showPopup()
                QTest.qWait(230)
                image = dialog.lang.view().window().grab().toImage()
                bottom_color = image.pixelColor(image.width() // 2, image.height() - 5)
                self.assertEqual(bottom_color.name(), colors["card"])
            finally:
                dialog.close()
                parent.close()


class TestFindMatchingSubtitle(unittest.TestCase):
    def test_exact_match_priority(self):
        """精确同名 {stem}.srt 优先于语言标签版本"""
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "movie.zh.srt").write_text("x", encoding="utf-8")
            (d / "movie.srt").write_text("x", encoding="utf-8")
            got = _find_matching_subtitle(d / "movie.mp4")
            self.assertEqual(got, d / "movie.srt")

    def test_language_tag_fallback(self):
        """无精确同名时匹配带语言标签的字幕"""
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "movie.zh.srt").write_text("x", encoding="utf-8")
            got = _find_matching_subtitle(d / "movie.mp4")
            self.assertEqual(got, d / "movie.zh.srt")

    def test_partial_srt_never_auto_selected(self):
        """回归：.partial.srt 断点文件不得被自动填入嵌入对话框

        上次运行失败/被停止后 partial 存在而成品 srt 缺失，旧逻辑 glob
        {stem}.*.srt 取排序第一个会把半截字幕嵌入并替换原视频。
        """
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "movie.partial.srt").write_text("x", encoding="utf-8")
            self.assertIsNone(_find_matching_subtitle(d / "movie.mp4"))
            # partial 与 zh.srt 共存时选 zh.srt（旧逻辑 p<z 会先选 partial）
            (d / "movie.zh.srt").write_text("x", encoding="utf-8")
            got = _find_matching_subtitle(d / "movie.mp4")
            self.assertEqual(got, d / "movie.zh.srt")

    def test_stem_with_glob_chars(self):
        """回归：文件名含 []（如 "movie [2024]"）不被当作 glob 通配符"""
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "movie [2024].zh.srt").write_text("x", encoding="utf-8")
            got = _find_matching_subtitle(d / "movie [2024].mp4")
            self.assertEqual(got, d / "movie [2024].zh.srt")


if __name__ == "__main__":
    unittest.main()
