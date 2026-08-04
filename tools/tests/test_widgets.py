#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""widgets.py 单元测试（仅测试非 Qt 依赖部分）"""
import unittest
from pathlib import Path

from subtitle_app.widgets import is_audio_file


class TestIsAudioFile(unittest.TestCase):
    """is_audio_file：扩展名判定（含大小写变体）"""

    def test_audio_and_video_extensions(self):
        cases = [
            ("test.mp3", True),
            ("test.wav", True),
            ("test.MP3", True),   # 大写扩展名
            ("test.Wav", True),   # 混合大小写
            ("test.mp4", False),
            ("test.srt", False),
        ]
        for name, expected in cases:
            with self.subTest(name=name):
                self.assertEqual(is_audio_file(Path(name)), expected)


class TestTrimLivePreview(unittest.TestCase):
    """panels._trim_live_preview：实时预览只保留最近若干块，避免全量重建卡顿"""

    def test_trims_to_keep_only_newest_blocks(self):
        from subtitle_app.panels import _trim_live_preview
        block = lambda i: f"{i}\n00:00:0{i},000 --> 00:00:0{i + 1},000\nseg_{i}"
        raw = "\n\n".join(block(i) for i in range(5))
        # 未超上限不裁剪
        self.assertEqual(_trim_live_preview(raw, max_blocks=10), raw)
        # 超过上限只保留最近 max_blocks 块
        out = _trim_live_preview(raw, max_blocks=2)
        self.assertEqual(out, "\n\n".join(block(i) for i in (3, 4)))


if __name__ == "__main__":
    unittest.main()
