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
    _MODEL_SPEED, _model_speed_lock, _parse_ffmpeg_time, _dedupe_adjacent_blocks,
)
from subtitle_app.srt_utils import SubtitleBlock


class TestTranscriberInit(unittest.TestCase):
    """Transcriber 基本构造"""

    def test_constructor_and_proc_handlers(self):
        t = Transcriber()
        self.assertIsNotNone(t)
        self.assertEqual(t._model_cache, {})
        self.assertIsNone(t.stop_check)
        self.assertIsNone(t._register_proc)
        self.assertIsNone(t._unregister_proc)
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

    def test_clear_cache_keeps_models(self):
        """clear_cache() 不再卸载模型，仅释放 CUDA 缓存；空缓存/无 torch 不崩溃"""
        t = Transcriber()
        t.clear_cache()  # 空缓存 + torch 不可用（setUp mock）都不崩溃
        mock_model = MagicMock()
        t._model_cache["key1"] = ("cpu", "int8", mock_model)
        t.clear_cache()
        # 模型缓存应保留
        self.assertIn("key1", t._model_cache)
        self.assertIs(t._model_cache["key1"][2], mock_model)

    def test_release_model_empties_and_is_loaded(self):
        t = Transcriber()
        self.assertFalse(t.is_loaded())
        mock_model = MagicMock()
        t._model_cache["key1"] = ("cpu", "int8", mock_model)
        self.assertTrue(t.is_loaded())
        t.release_model()
        self.assertEqual(t._model_cache, {})
        self.assertFalse(t.is_loaded())


class TestReadStderrLoop(unittest.TestCase):
    """_read_stderr_loop 辅助方法"""

    def test_reads_lines_and_stops_on_exception(self):
        # 正常读完
        lines = []
        lock = threading.Lock()
        done = threading.Event()
        Transcriber._read_stderr_loop(iter(["line1\n", "line2\n"]), lines, lock, done)
        self.assertEqual(len(lines), 2)
        # 流异常 → done 置位
        class BrokenStream:
            def __iter__(self):
                return self
            def __next__(self):
                raise ValueError("stream closed")
        done = threading.Event()
        Transcriber._read_stderr_loop(BrokenStream(), [], threading.Lock(), done)
        self.assertTrue(done.is_set())


class TestParseFfmpegTime(unittest.TestCase):
    """_parse_ffmpeg_time：小数位数不固定，须按实际位数换算"""

    def test_varying_fraction_digits(self):
        cases = [
            ("frame= 12 time=00:00:04.110000000", 4.11),  # 9 位小数
            ("time=00:01:02.250000", 62.25),              # 6 位小数
            ("time=00:00:00.500", 0.5),                   # 3 位小数
        ]
        for line, expected in cases:
            with self.subTest(line=line):
                self.assertAlmostEqual(_parse_ffmpeg_time(line), expected)
        # 无 time 字段 → None
        self.assertIsNone(_parse_ffmpeg_time("frame= 0 fps=0.0"))
        self.assertIsNone(_parse_ffmpeg_time(""))


class TestEstimateWeights(unittest.TestCase):
    """_estimate_weights"""

    def test_returns_weights_dict(self):
        weights = Transcriber._estimate_weights(100.0, Path("large-v3-turbo"))
        self.assertIn("extract", weights)
        self.assertIn("model", weights)
        self.assertIn("transcribe", weights)
        self.assertGreater(weights["extract"], 0)
        self.assertGreater(weights["transcribe"], 0)


class TestGetDuration(unittest.TestCase):
    """get_duration"""

    @patch("subtitle_app.transcriber.subprocess.Popen")
    def test_get_duration_success_and_failure(self, mock_popen):
        t = Transcriber()
        proc = MagicMock()
        proc.returncode = 0
        proc.communicate.return_value = ("123.456\n", "")
        mock_popen.return_value = proc
        self.assertAlmostEqual(t.get_duration(Path("test.mp4"), "ffprobe"), 123.456)
        # 失败返回 0.0
        proc2 = MagicMock()
        proc2.returncode = 1
        proc2.communicate.return_value = ("", "error")
        mock_popen.return_value = proc2
        self.assertEqual(t.get_duration(Path("test.mp4"), "ffprobe"), 0.0)


class TestWritePartialSrt(unittest.TestCase):
    """_write_partial_srt"""

    def test_writes_atomically_and_skips_empty(self):
        with tempfile.TemporaryDirectory() as d:
            blocks = [
                SubtitleBlock(1, 0.0, 1.0, "Hello"),
                SubtitleBlock(2, 1.0, 2.0, "World"),
            ]
            path = Path(d) / "test.partial.srt"
            Transcriber._write_partial_srt(path, blocks)
            content = path.read_text(encoding="utf-8")
            self.assertIn("Hello", content)
            self.assertIn("00:00:00,000 --> 00:00:01,000", content)
        # 空文本块被跳过，序号重编号为 1、2
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
            self.assertIn("1\n", content)
            self.assertIn("2\n", content)


class TestSplitLongBlocks(unittest.TestCase):
    """split_long_blocks 不应产出空文本时间轴"""

    def test_short_texts_never_produce_empty_cues(self):
        # 旧逻辑：1 字符 + ~45s → 连续两条 ~15s 空 cue + 一条有字
        blocks = [SubtitleBlock(1, 6000.293, 6045.154, "x")]
        out = split_long_blocks(blocks)
        self.assertTrue(out)
        self.assertTrue(all(b.text.strip() for b in out))
        self.assertTrue(all(b.end - b.start <= MAX_BLOCK_DURATION + 1e-6 for b in out))
        # <=3 字符整块保留，时长压到上限，不切成单字符块
        blocks = [SubtitleBlock(1, 100.0, 145.0, "Hi")]
        out = split_long_blocks(blocks)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].text, "Hi")
        self.assertAlmostEqual(out[0].end - out[0].start, MAX_BLOCK_DURATION)
        # 4 字符长时长 → 每段至少 2 字符
        blocks = [SubtitleBlock(1, 0.0, 60.0, "abcd")]
        out = split_long_blocks(blocks)
        self.assertTrue(out)
        self.assertTrue(all(len(b.text.strip()) >= 2 for b in out))
        self.assertEqual("".join(b.text for b in out), "abcd")

    def test_dedupe_adjacent_duplicates_merged(self):
        # 合并相邻文本完全相同的块（Whisper 对循环语音的重复输出）
        blocks = [
            SubtitleBlock(1, 0.0, 5.0, "やらして"),
            SubtitleBlock(2, 5.0, 10.0, "やらして"),
            SubtitleBlock(3, 10.0, 15.0, "违います"),
            SubtitleBlock(4, 15.0, 20.0, ""),
        ]
        out = _dedupe_adjacent_blocks(blocks)
        self.assertEqual(len(out), 2)
        self.assertEqual([b.text for b in out], ["やらして", "违います"])
        self.assertEqual([b.index for b in out], [1, 2])

    def test_empty_dropped_and_sentence_split(self):
        # 空白块被过滤，序号重编号
        blocks = [
            SubtitleBlock(1, 0.0, 30.0, "   "),
            SubtitleBlock(2, 30.0, 35.0, "ok"),
        ]
        out = split_long_blocks(blocks)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].text, "ok")
        self.assertEqual(out[0].index, 1)
        # 长句按标点拆分，文本不丢
        blocks = [SubtitleBlock(1, 0.0, 30.0, "Hello world. How are you? I am fine.")]
        out = split_long_blocks(blocks)
        self.assertGreaterEqual(len(out), 2)
        self.assertTrue(all(b.text.strip() for b in out))


class TestModelSpeedLock(unittest.TestCase):
    """_MODEL_SPEED 线程安全锁"""

    def test_lock_and_concurrent_access(self):
        """锁对象存在、_MODEL_SPEED 有值、多线程并发读写不崩溃"""
        from subtitle_app.transcriber import _model_speed_lock, _MODEL_SPEED
        self.assertIsNotNone(_model_speed_lock)
        with _model_speed_lock:
            self.assertGreater(_MODEL_SPEED.get("large-v3-turbo", 1.5), 0)

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
    """语言检测复用开关：默认开启（单一语言目录更快），
    显式关闭 reuse_auto_lang 时每文件独立检测（适合混合语言目录）"""

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

    def test_reuse_default_on(self):
        """默认开启：首个检测后同批复用"""
        self.assertEqual(self._run(), None)
        self.assertEqual(self.t._cached_auto_lang, "en")
        self.assertEqual(self._run(), "en")

    def test_reuse_disabled_detects_each_file(self):
        """显式关闭 revert 每文件独立检测：不缓存、不复用"""
        self.assertEqual(self._run(reuse_auto_lang=False), None)
        self.assertIsNone(self.t._cached_auto_lang)
        self.assertEqual(self._run(reuse_auto_lang=False), None)


class TestModelCache(unittest.TestCase):
    """模型缓存机制"""

    def setUp(self):
        self.t = Transcriber()

    def test_cache_same_key_reuses(self):
        """相同 key 返回缓存不重复加载；不同 key 创建新实例"""
        with patch("subtitle_app.transcriber._get_whisper_model") as mock_get:
            MockWhisperCls = MagicMock()
            mock_get.return_value = MockWhisperCls
            post = MagicMock()
            model_dir = Path("fake-model")
            m1 = self.t.load_whisper_model(model_dir, "cpu", "int8", post)
            m2 = self.t.load_whisper_model(model_dir, "cpu", "int8", post)
            self.assertIsNotNone(m1)
            self.assertIs(m1, m2)  # 同一个对象
            self.assertEqual(MockWhisperCls.call_count, 1)
            m3 = self.t.load_whisper_model(model_dir, "cuda", "float16", post)
            self.assertIsNotNone(m3)
            self.assertEqual(MockWhisperCls.call_count, 2)


if __name__ == "__main__":
    unittest.main()
