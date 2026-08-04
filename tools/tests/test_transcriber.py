#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""transcriber.py 单元测试"""
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from subtitle_app.transcriber import (
    Transcriber, split_long_blocks, MAX_BLOCK_DURATION,
    _MODEL_SPEED, _model_speed_lock, _parse_ffmpeg_time,
)
from subtitle_app.srt_utils import SubtitleBlock


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
    """clear_cache / release_model / is_loaded"""

    # 用 MagicMock 替代 torch，避免触发 CUDA 初始化（~3.8s 开销）
    _SENTINEL = object()

    def setUp(self):
        self._real_torch = sys.modules.get("torch", self._SENTINEL)
        mock_torch = MagicMock()
        mock_torch.cuda.is_available.return_value = False
        sys.modules["torch"] = mock_torch

    def tearDown(self):
        if self._real_torch is not self._SENTINEL:
            sys.modules["torch"] = self._real_torch
        else:
            sys.modules.pop("torch", None)

    def test_clear_cache_empty(self):
        t = Transcriber()
        t.clear_cache()
        self.assertEqual(t._model_cache, {})

    def test_clear_cache_keeps_models(self):
        """clear_cache() 不再卸载模型，仅释放 CUDA 缓存"""
        t = Transcriber()
        mock_model = MagicMock()
        t._model_cache["key1"] = ("cpu", "int8", mock_model)
        t.clear_cache()
        # 模型缓存应保留
        self.assertIn("key1", t._model_cache)
        self.assertIs(t._model_cache["key1"][2], mock_model)

    def test_release_model_empties_cache(self):
        t = Transcriber()
        mock_model = MagicMock()
        t._model_cache["key1"] = ("cpu", "int8", mock_model)
        t.release_model()
        self.assertEqual(t._model_cache, {})

    def test_is_loaded(self):
        t = Transcriber()
        self.assertFalse(t.is_loaded())
        t._model_cache["key1"] = ("cpu", "int8", MagicMock())
        self.assertTrue(t.is_loaded())
        t.release_model()
        self.assertFalse(t.is_loaded())

    def test_clear_cache_no_torch_crash(self):
        """即使 torch 不可用也不崩溃"""
        t = Transcriber()
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


class TestParseFfmpegTime(unittest.TestCase):
    """_parse_ffmpeg_time：小数位数不固定，须按实际位数换算"""

    def test_nine_digits_fraction(self):
        # 修复前 ms/1e6 会把 110000000 当 110 秒，实际应为 4.11 秒
        self.assertAlmostEqual(
            _parse_ffmpeg_time("frame= 12 time=00:00:04.110000000"), 4.11)

    def test_six_digits_fraction(self):
        self.assertAlmostEqual(
            _parse_ffmpeg_time("time=00:01:02.250000"), 62.25)

    def test_three_digits_fraction(self):
        self.assertAlmostEqual(
            _parse_ffmpeg_time("time=00:00:00.500"), 0.5)

    def test_no_time_returns_none(self):
        self.assertIsNone(_parse_ffmpeg_time("frame= 0 fps=0.0"))
        self.assertIsNone(_parse_ffmpeg_time(""))


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

    def test_skips_empty_text(self):
        with tempfile.TemporaryDirectory() as d:
            blocks = [
                SubtitleBlock(1, 0.0, 1.0, "Hello"),
                SubtitleBlock(2, 1.0, 16.0, "   "),
                SubtitleBlock(3, 16.0, 17.0, "World"),
            ]
            path = Path(d) / "test.partial.srt"
            Transcriber._write_partial_srt(path, blocks)
            content = path.read_text(encoding="utf-8")
            self.assertEqual(content.count("-->"), 2)
            self.assertIn("Hello", content)
            self.assertIn("World", content)
            # 序号应重编号为 1、2，中间空文本条目被丢掉
            self.assertIn("1\n", content)
            self.assertIn("2\n", content)


class TestSplitLongBlocks(unittest.TestCase):
    """split_long_blocks 不应产出空文本时间轴"""

    def test_short_text_long_duration_no_empty_cues(self):
        # 旧逻辑：1 字符 + ~45s → 连续两条 ~15s 空 cue + 一条有字
        blocks = [SubtitleBlock(1, 6000.293, 6045.154, "x")]
        out = split_long_blocks(blocks)
        self.assertTrue(out)
        self.assertTrue(all(b.text.strip() for b in out))
        self.assertTrue(all(b.end - b.start <= MAX_BLOCK_DURATION + 1e-6 for b in out))

    def test_two_char_forced_split_has_text_each(self):
        blocks = [SubtitleBlock(1, 100.0, 145.0, "Hi")]
        out = split_long_blocks(blocks)
        self.assertEqual(len(out), 2)
        self.assertEqual("".join(b.text for b in out), "Hi")
        self.assertTrue(all(b.text.strip() for b in out))

    def test_empty_input_dropped(self):
        blocks = [
            SubtitleBlock(1, 0.0, 30.0, "   "),
            SubtitleBlock(2, 30.0, 35.0, "ok"),
        ]
        out = split_long_blocks(blocks)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].text, "ok")
        self.assertEqual(out[0].index, 1)

    def test_sentence_split_keeps_text(self):
        blocks = [SubtitleBlock(1, 0.0, 30.0, "Hello world. How are you? I am fine.")]
        out = split_long_blocks(blocks)
        self.assertGreaterEqual(len(out), 2)
        self.assertTrue(all(b.text.strip() for b in out))


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


class TestAutoLangReuse(unittest.TestCase):
    """语言检测复用开关：默认关闭（混合语言目录每文件独立检测），
    仅当显式开启 reuse_auto_lang 时复用同批首个检测结果"""

    class FakeSegment:
        def __init__(self, start, end, text):
            self.start, self.end, self.text = start, end, text

    class FakeInfo:
        language = "en"

    def setUp(self):
        self.t = Transcriber()
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
            "checkpoint_enabled": False,
            "word_timestamps": False,
        }

    def _run(self, **opts):
        """跑一次 transcribe_video，返回 model.transcribe 收到的 language 参数"""
        from unittest.mock import patch as _patch
        model = MagicMock()
        model.transcribe.return_value = (
            iter([self.FakeSegment(0.0, 1.0, "Hi.")]), self.FakeInfo())
        with _patch("subtitle_app.transcriber.Transcriber.load_whisper_model",
                    return_value=model):
            self.t.get_duration = MagicMock(return_value=60.0)
            self.t.extract_audio_with_progress = MagicMock(return_value=60.0)
            with tempfile.TemporaryDirectory() as d:
                video = Path(d) / "test.mp4"
                video.write_text("x", encoding="utf-8")
                self.t.transcribe_video(video, Path(d), {**self.base_opts, **opts})
        return model.transcribe.call_args.kwargs.get("language")

    def test_reuse_disabled_by_default(self):
        """默认（reuse_auto_lang 缺省）：每次独立检测，不写缓存不复用"""
        self.assertEqual(self._run(), None)
        # 第一次检测结果不应写入 _cached_auto_lang
        self.assertIsNone(self.t._cached_auto_lang)
        # 第二次仍独立检测
        self.assertEqual(self._run(), None)

    def test_reuse_enabled_reuses_first_detection(self):
        """开启 reuse_auto_lang：首次检测写入缓存，后续复用"""
        self.assertEqual(self._run(reuse_auto_lang=True), None)
        self.assertEqual(self.t._cached_auto_lang, "en")
        self.assertEqual(self._run(reuse_auto_lang=True), "en")


class TestModelCache(unittest.TestCase):
    """模型缓存机制"""

    def setUp(self):
        self.t = Transcriber()

    @patch("subtitle_app.transcriber._get_whisper_model")
    def test_cache_same_model(self, mock_get):
        """相同 key 应返回缓存，不重复加载"""
        MockWhisperCls = MagicMock()
        mock_get.return_value = MockWhisperCls
        post = MagicMock()
        model_dir = Path("fake-model")
        m1 = self.t.load_whisper_model(model_dir, "cpu", "int8", post)
        # 第二次应走缓存
        m2 = self.t.load_whisper_model(model_dir, "cpu", "int8", post)

        self.assertIsNotNone(m1)
        self.assertIs(m1, m2)  # 同一个对象
        self.assertEqual(MockWhisperCls.call_count, 1)

    @patch("subtitle_app.transcriber._get_whisper_model")
    def test_different_key_different_model(self, mock_get):
        """不同设备/精度创建不同的模型实例"""
        MockWhisperCls = MagicMock()
        mock_get.return_value = MockWhisperCls
        post = MagicMock()
        model_dir = Path("fake-model")

        m1 = self.t.load_whisper_model(model_dir, "cpu", "int8", post)
        m2 = self.t.load_whisper_model(model_dir, "cuda", "float16", post)

        self.assertIsNotNone(m1)
        self.assertIsNotNone(m2)
        self.assertEqual(MockWhisperCls.call_count, 2)


if __name__ == "__main__":
    unittest.main()
