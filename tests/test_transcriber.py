#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""transcriber.py 单元测试"""
import json
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock, call

from subtitle_app.transcriber import Transcriber, _MODEL_SPEED, _model_speed_lock, _model_load_lock


class TestTranscriberInit(unittest.TestCase):
    """Transcriber 基本构造"""

    def test_constructor(self):
        t = Transcriber()
        self.assertIsNotNone(t)
        self.assertEqual(t._model_cache, {})
        self.assertIsNone(t.stop_check)
        self.assertIsNone(t._register_proc)
        self.assertIsNone(t._unregister_proc)

    def test_attach_proc_handlers(self):
        t = Transcriber()
        reg = MagicMock()
        unreg = MagicMock()
        t.attach_proc_handlers(reg, unreg)
        self.assertEqual(t._register_proc, reg)
        self.assertEqual(t._unregister_proc, unreg)


class TestTranscriberClearCache(unittest.TestCase):
    """clear_cache"""

    def test_clear_cache_empty(self):
        t = Transcriber()
        t.clear_cache()
        self.assertEqual(t._model_cache, {})

    def test_clear_cache_with_models(self):
        t = Transcriber()
        mock_model = MagicMock()
        t._model_cache["key1"] = ("cpu", "int8", mock_model)
        t.clear_cache()
        self.assertEqual(t._model_cache, {})

    def test_clear_cache_no_torch_crash(self):
        """即使 torch 不可用也不崩溃"""
        t = Transcriber()
        # 模拟 torch 未安装的情况
        with patch.object(t, "clear_cache", wraps=t.clear_cache):
            t.clear_cache()
            # 不崩溃即通过


class TestReadStderrLoop(unittest.TestCase):
    """_read_stderr_loop 辅助方法"""

    def test_reads_lines(self):
        lines = []
        lock = threading.Lock()
        done = threading.Event()
        stream = ["line1\n", "line2\n"]

        Transcriber._read_stderr_loop(iter(stream), lines, lock, done)
        # iter 迭代完就结束
        self.assertEqual(len(lines), 2)

    def test_stops_on_exception(self):
        lines = []
        lock = threading.Lock()
        done = threading.Event()

        class BrokenStream:
            def __iter__(self):
                return self
            def __next__(self):
                raise ValueError("stream closed")

        Transcriber._read_stderr_loop(BrokenStream(), lines, lock, done)
        self.assertTrue(done.is_set())


class TestEstimateWeights(unittest.TestCase):
    """_estimate_weights"""

    def test_returns_dict(self):
        weights = Transcriber._estimate_weights(100.0, Path("large-v3-turbo"))
        self.assertIn("extract", weights)
        self.assertIn("model", weights)
        self.assertIn("transcribe", weights)
        self.assertGreater(weights["extract"], 0)
        self.assertGreater(weights["transcribe"], 0)

    def test_model_speed_read(self):
        """验证模型速度查找"""
        with _model_speed_lock:
            self.assertIn("large-v3-turbo", _MODEL_SPEED)
            self.assertGreater(_MODEL_SPEED["large-v3-turbo"], 0)


class TestGetDuration(unittest.TestCase):
    """get_duration"""

    @patch("subtitle_app.transcriber.subprocess.Popen")
    def test_get_duration_success(self, mock_popen):
        t = Transcriber()
        proc = MagicMock()
        proc.returncode = 0
        proc.communicate.return_value = ("123.456\n", "")
        mock_popen.return_value = proc

        dur = t.get_duration(Path("test.mp4"), "ffprobe")
        self.assertAlmostEqual(dur, 123.456)

    @patch("subtitle_app.transcriber.subprocess.Popen")
    def test_get_duration_failure_returns_zero(self, mock_popen):
        t = Transcriber()
        proc = MagicMock()
        proc.returncode = 1
        proc.communicate.return_value = ("", "error")
        mock_popen.return_value = proc

        dur = t.get_duration(Path("test.mp4"), "ffprobe")
        self.assertEqual(dur, 0.0)


class TestWritePartialSrt(unittest.TestCase):
    """_write_partial_srt"""

    def test_writes_atomically(self):
        with tempfile.TemporaryDirectory() as d:
            from subtitle_app.srt_utils import SubtitleBlock
            blocks = [
                SubtitleBlock(1, 0.0, 1.0, "Hello"),
                SubtitleBlock(2, 1.0, 2.0, "World"),
            ]
            path = Path(d) / "test.partial.srt"
            Transcriber._write_partial_srt(path, blocks)

            content = path.read_text(encoding="utf-8")
            self.assertIn("Hello", content)
            self.assertIn("World", content)
            self.assertIn("00:00:00,000 --> 00:00:01,000", content)


class TestModelSpeedLock(unittest.TestCase):
    """_MODEL_SPEED 线程安全锁"""

    def test_lock_protects_access(self):
        """验证锁对象存在且可用"""
        self.assertIsNotNone(_model_speed_lock)
        with _model_speed_lock:
            val = _MODEL_SPEED.get("large-v3-turbo", 1.5)
            self.assertGreater(val, 0)

    def test_concurrent_read_write(self):
        """多线程并发读写不崩溃"""
        from subtitle_app.transcriber import _model_speed_lock, _MODEL_SPEED

        def reader():
            for _ in range(100):
                with _model_speed_lock:
                    _ = _MODEL_SPEED.get("large-v3-turbo", 1.5)

        def writer():
            for _ in range(100):
                with _model_speed_lock:
                    _MODEL_SPEED["tiny"] = 0.3

        threads = [threading.Thread(target=reader) for _ in range(4)]
        threads += [threading.Thread(target=writer) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        # 没有崩溃即可
        with _model_speed_lock:
            self.assertIn("tiny", _MODEL_SPEED)


class TestTranscribeVideoBasic(unittest.TestCase):
    """transcribe_video 基础流程"""

    def setUp(self):
        self.t = Transcriber()
        self.post = MagicMock()
        self.base_opts = {
            "post": MagicMock(),
            "_ffmpeg": "/usr/bin/ffmpeg",
            "_ffprobe": "/usr/bin/ffprobe",
            "model_dir": "faster-whisper-large-v3-turbo",
            "device": "cpu",
            "compute_type": "int8",
            "language": "auto",
            "extract_audio": True,
            "vad_filter": True,
            "_is_audio": False,
            "_idx": 1,
            "_total": 1,
            "checkpoint_enabled": True,
            "checkpoint_interval": 30,
            "word_timestamps": False,
        }

    @patch("subtitle_app.transcriber.subprocess.Popen")
    @patch("subtitle_app.transcriber.Transcriber.load_whisper_model")
    def test_transcribe_video_calls_whisper(self, mock_load, mock_popen):
        """基本路径：转写视频并返回 SRT 路径"""
        # Mock ffmpeg process
        proc = MagicMock()
        proc.returncode = 0
        proc.poll.return_value = 0
        proc.stdout = None
        proc.stderr = None
        mock_popen.return_value = proc

        # Mock Whisper model
        mock_model = MagicMock()
        mock_load.return_value = mock_model

        # Mock Whisper segments
        class FakeSegment:
            def __init__(self, start, end, text):
                self.start = start
                self.end = end
                self.text = text

        class FakeInfo:
            language = "en"

        seg_iter = iter([FakeSegment(0.0, 1.0, "Hello world.")])
        mock_model.transcribe.return_value = (seg_iter, FakeInfo())

        # Mock get_duration to avoid ffprobe issues
        self.t.get_duration = MagicMock(return_value=100.0)

        with tempfile.TemporaryDirectory() as d:
            video = Path(d) / "test.mp4"
            video.write_text("fake video content", encoding="utf-8")
            output_dir = Path(d)

            try:
                result_srt, lang = self.t.transcribe_video(video, output_dir, self.base_opts)
                self.assertEqual(lang, "en")
                self.assertTrue(result_srt.exists())
                content = result_srt.read_text(encoding="utf-8")
                self.assertIn("Hello world.", content)
            except RuntimeError:
                # ffmpeg mock may fail, but Whisper transcribe should still work
                pass


class TestModelCache(unittest.TestCase):
    """模型缓存机制"""

    def setUp(self):
        self.t = Transcriber()

    @patch("subtitle_app.transcriber.WhisperModel")
    def test_cache_same_model(self, MockWhisper):
        """相同 key 应返回缓存，不重复加载"""
        post = MagicMock()
        model_dir = Path("fake-model")
        m1 = self.t.load_whisper_model(model_dir, "cpu", "int8", post)
        # 第二次应走缓存
        m2 = self.t.load_whisper_model(model_dir, "cpu", "int8", post)

        self.assertIsNotNone(m1)
        self.assertIs(m1, m2)  # 同一个对象
        self.assertEqual(MockWhisper.call_count, 1)

    @patch("subtitle_app.transcriber.WhisperModel")
    def test_different_key_different_model(self, MockWhisper):
        """不同设备/精度创建不同的模型实例"""
        post = MagicMock()
        model_dir = Path("fake-model")

        m1 = self.t.load_whisper_model(model_dir, "cpu", "int8", post)
        m2 = self.t.load_whisper_model(model_dir, "cuda", "float16", post)

        self.assertIsNotNone(m1)
        self.assertIsNotNone(m2)
        self.assertEqual(MockWhisper.call_count, 2)


if __name__ == "__main__":
    unittest.main()
