#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""widgets.py 单元测试（仅测试非 Qt 依赖部分）"""
import unittest
from pathlib import Path

from subtitle_app.widgets import is_audio_file


class TestIsAudioFile(unittest.TestCase):
    """is_audio_file"""

    def test_mp3_is_audio(self):
        self.assertTrue(is_audio_file(Path("test.mp3")))

    def test_wav_is_audio(self):
        self.assertTrue(is_audio_file(Path("test.wav")))

    def test_mp4_is_not_audio(self):
        self.assertFalse(is_audio_file(Path("test.mp4")))

    def test_srt_is_not_audio(self):
        self.assertFalse(is_audio_file(Path("test.srt")))

    def test_uppercase_ext(self):
        self.assertTrue(is_audio_file(Path("test.MP3")))

    def test_mixed_case(self):
        self.assertTrue(is_audio_file(Path("test.Wav")))


class TestTrimLivePreview(unittest.TestCase):
    """panels._trim_live_preview：实时预览只保留最近若干块，避免全量重建卡顿"""

    def _block(self, i):
        return f"{i}\n00:00:0{i},000 --> 00:00:0{i+1},000\nseg_{i}"

    def test_under_limit_unchanged(self):
        from subtitle_app.panels import _trim_live_preview
        raw = "\n\n".join(self._block(i) for i in range(3))
        self.assertEqual(_trim_live_preview(raw), raw)

    def test_over_limit_keeps_newest(self):
        from subtitle_app.panels import _trim_live_preview
        raw = "\n\n".join(self._block(i) for i in range(5))
        out = _trim_live_preview(raw, max_blocks=2)
        self.assertNotIn("seg_0", out)
        self.assertNotIn("seg_1", out)
        self.assertIn("seg_3", out)
        self.assertIn("seg_4", out)


if __name__ == "__main__":
    unittest.main()