#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""widgets.py 单元测试（仅测试非 Qt 依赖部分）"""
import unittest
from pathlib import Path
from unittest.mock import patch

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


if __name__ == "__main__":
    unittest.main()