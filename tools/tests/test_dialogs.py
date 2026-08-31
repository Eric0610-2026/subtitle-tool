#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""测试 dialogs 的纯函数：嵌入对话框字幕匹配（_find_matching_subtitle）"""
import tempfile
import unittest
from pathlib import Path

from subtitle_app.dialogs import _find_matching_subtitle


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