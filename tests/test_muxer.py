#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""muxer.py 单元测试"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch, call

from subtitle_app.srt_utils import SubtitleBlock


class TestSanitizeSrtForMux(unittest.TestCase):
    """_sanitize_srt_for_mux"""

    def _make_srt(self, path: Path, blocks=None):
        if blocks is None:
            blocks = [SubtitleBlock(1, 1.0, 3.0, "Hello"),
                      SubtitleBlock(2, 3.5, 6.0, "World")]
        from subtitle_app.srt_utils import write_srt
        write_srt(path, blocks, [b.text for b in blocks])
        return path

    def test_already_clean_returns_same_path(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "test.srt"
            self._make_srt(p)
            from subtitle_app.muxer import _sanitize_srt_for_mux
            result = _sanitize_srt_for_mux(p)
            self.assertEqual(result, p)  # 未修改，返回原路径

    def test_empty_file_returns_same_path(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "empty.srt"
            p.write_text("", encoding="utf-8")
            from subtitle_app.muxer import _sanitize_srt_for_mux
            result = _sanitize_srt_for_mux(p)
            self.assertEqual(result, p)

    def test_nonexistent_file_returns_same_path(self):
        """不存在的文件应返回原路径（在 parse_srt 中抛出异常）"""
        from subtitle_app.muxer import _sanitize_srt_for_mux
        result = _sanitize_srt_for_mux(Path("nonexistent.srt"))
        self.assertEqual(result, Path("nonexistent.srt"))


class TestFindSiblingProbe(unittest.TestCase):
    """_find_sibling_probe"""

    def test_finds_ffprobe_exe(self):
        with tempfile.TemporaryDirectory() as d:
            ffmpeg = Path(d) / "ffmpeg.exe"
            ffmpeg.write_text("dummy")
            ffprobe = Path(d) / "ffprobe.exe"
            ffprobe.write_text("dummy")
            from subtitle_app.muxer import _find_sibling_probe
            result = _find_sibling_probe(str(ffmpeg))
            self.assertEqual(result, str(ffprobe))

    def test_finds_ffprobe_without_ext(self):
        with tempfile.TemporaryDirectory() as d:
            ffmpeg = Path(d) / "ffmpeg"
            ffmpeg.write_text("dummy")
            ffprobe = Path(d) / "ffprobe"
            ffprobe.write_text("dummy")
            from subtitle_app.muxer import _find_sibling_probe
            result = _find_sibling_probe(str(ffmpeg))
            self.assertEqual(result, str(ffprobe))

    def test_no_ffprobe_returns_none(self):
        with tempfile.TemporaryDirectory() as d:
            ffmpeg = Path(d) / "ffmpeg.exe"
            ffmpeg.write_text("dummy")
            from subtitle_app.muxer import _find_sibling_probe
            result = _find_sibling_probe(str(ffmpeg))
            self.assertIsNone(result)


class TestCountExistingSubStreams(unittest.TestCase):
    """_count_existing_sub_streams"""

    @patch("subtitle_app.muxer.subprocess.run")
    def test_returns_count(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0,
                                          stdout="0\n1\n2\n")
        from subtitle_app.muxer import _count_existing_sub_streams
        result = _count_existing_sub_streams(Path("test.mp4"), "ffprobe.exe")
        self.assertEqual(result, 3)

    @patch("subtitle_app.muxer.subprocess.run")
    def test_no_ffprobe_returns_zero(self, mock_run):
        from subtitle_app.muxer import _count_existing_sub_streams
        result = _count_existing_sub_streams(Path("test.mp4"), None)
        self.assertEqual(result, 0)
        mock_run.assert_not_called()

    @patch("subtitle_app.muxer.subprocess.run")
    def test_error_returns_zero(self, mock_run):
        mock_run.side_effect = FileNotFoundError
        from subtitle_app.muxer import _count_existing_sub_streams
        result = _count_existing_sub_streams(Path("test.mp4"), "ffprobe.exe")
        self.assertEqual(result, 0)


class TestProbeDuration(unittest.TestCase):
    """_probe_duration"""

    @patch("subtitle_app.muxer.subprocess.run")
    def test_returns_duration(self, mock_run):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "test.mp4"
            p.write_text("dummy")
            mock_run.return_value = MagicMock(returncode=0,
                                              stdout="123.456\n")
            from subtitle_app.muxer import _probe_duration
            result = _probe_duration(p, "ffprobe.exe")
            self.assertAlmostEqual(result, 123.456)

    @patch("subtitle_app.muxer.subprocess.run")
    def test_no_ffprobe_returns_none(self, mock_run):
        from subtitle_app.muxer import _probe_duration
        result = _probe_duration(Path("test.mp4"), None)
        self.assertIsNone(result)
        mock_run.assert_not_called()

    @patch("subtitle_app.muxer.subprocess.run")
    def test_na_stdout_returns_none(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0,
                                          stdout="N/A\n")
        from subtitle_app.muxer import _probe_duration
        result = _probe_duration(Path("test.mp4"), "ffprobe.exe")
        self.assertIsNone(result)

    @patch("subtitle_app.muxer.subprocess.run")
    def test_nonexistent_file_returns_none(self, mock_run):
        from subtitle_app.muxer import _probe_duration
        result = _probe_duration(Path("nonexistent.mp4"), "ffprobe.exe")
        self.assertIsNone(result)
        mock_run.assert_not_called()

    @patch("subtitle_app.muxer.subprocess.run")
    def test_error_returns_none(self, mock_run):
        mock_run.side_effect = FileNotFoundError
        from subtitle_app.muxer import _probe_duration
        result = _probe_duration(Path("test.mp4"), "ffprobe.exe")
        self.assertIsNone(result)


class TestVerifyDuration(unittest.TestCase):
    """_verify_duration"""

    def setUp(self):
        self._probe_patcher = patch("subtitle_app.muxer._probe_duration")
        self.mock_probe = self._probe_patcher.start()
        self._sibling_patcher = patch("subtitle_app.muxer._find_sibling_probe")
        self.mock_sibling = self._sibling_patcher.start()
        self.mock_sibling.return_value = "ffprobe.exe"

    def tearDown(self):
        self._probe_patcher.stop()
        self._sibling_patcher.stop()

    def test_passed_ratio_ok(self):
        self.mock_probe.side_effect = [100.0, 98.0]  # src=100, out=98
        from subtitle_app.muxer import _verify_duration
        passed, msg = _verify_duration(Path("test.mp4"), Path("test.mkv"),
                                       "ffmpeg.exe")
        self.assertTrue(passed)
        self.assertIn("98%", msg)

    def test_failed_ratio_low(self):
        self.mock_probe.side_effect = [100.0, 50.0]  # 50% < 95%
        from subtitle_app.muxer import _verify_duration
        passed, msg = _verify_duration(Path("test.mp4"), Path("test.mkv"),
                                       "ffmpeg.exe")
        self.assertFalse(passed)
        self.assertIn("50%", msg)

    def test_short_video_skips(self):
        self.mock_probe.side_effect = [5.0, 5.0]  # < 10s
        from subtitle_app.muxer import _verify_duration
        passed, msg = _verify_duration(Path("test.mp4"), Path("test.mkv"),
                                       "ffmpeg.exe")
        self.assertTrue(passed)

    def test_no_ffprobe_fails(self):
        self.mock_sibling.return_value = None
        from subtitle_app.muxer import _verify_duration
        passed, msg = _verify_duration(Path("test.mp4"), Path("test.mkv"),
                                       "ffmpeg.exe")
        self.assertFalse(passed)

    def test_missing_duration_fails(self):
        self.mock_probe.side_effect = [None, 50.0]
        from subtitle_app.muxer import _verify_duration
        passed, msg = _verify_duration(Path("test.mp4"), Path("test.mkv"),
                                       "ffmpeg.exe")
        self.assertFalse(passed)


class TestBuildEmbedCmd(unittest.TestCase):
    """_build_embed_cmd"""

    def test_basic_structure(self):
        with tempfile.TemporaryDirectory() as d:
            video = Path(d) / "test.mp4"
            srt = Path(d) / "test.srt"
            mkv = Path(d) / "test.mkv"
            from subtitle_app.muxer import _build_embed_cmd
            cmd = _build_embed_cmd("ffmpeg.exe", video, srt, mkv)
            self.assertIsInstance(cmd, list)
            self.assertEqual(cmd[0], "ffmpeg.exe")
            self.assertIn("-y", cmd)
            self.assertIn(str(video), cmd)
            self.assertIn(str(srt), cmd)
            self.assertIn(str(mkv), cmd)
            # 输入参数个数
            self.assertIn("-c:v", cmd)
            self.assertIn("copy", cmd)

    def test_map_arguments(self):
        from subtitle_app.muxer import _build_embed_cmd
        cmd = _build_embed_cmd("ffmpeg", Path("v.mp4"), Path("v.srt"),
                               Path("v.mkv"))
        # 应该包含 -map 0:v? -map 0:a? -map 1
        idx_v = cmd.index("-map")
        self.assertEqual(cmd[idx_v + 1], "0:v?")
        self.assertEqual(cmd[idx_v + 3], "0:a?")
        self.assertEqual(cmd[idx_v + 5], "1")


class TestCleanupFile(unittest.TestCase):
    """_cleanup_file"""

    def test_none_does_nothing(self):
        from subtitle_app.muxer import _cleanup_file
        # 不应抛出异常
        _cleanup_file(None)

    def test_nonexistent_does_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "nonexistent.tmp"
            from subtitle_app.muxer import _cleanup_file
            _cleanup_file(p)  # 不应抛出异常

    def test_removes_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "test.tmp"
            p.write_text("dummy")
            self.assertTrue(p.exists())
            from subtitle_app.muxer import _cleanup_file
            _cleanup_file(p)
            self.assertFalse(p.exists())


class TestLogFfmpegError(unittest.TestCase):
    """_log_ffmpeg_error"""

    def test_posts_error_messages(self):
        posts = []
        proc = MagicMock()
        proc.returncode = 1
        from subtitle_app.muxer import _log_ffmpeg_error
        _log_ffmpeg_error(posts.append, "ffmpeg -i test.mp4", proc,
                          "error: something went wrong")
        # 应该至少有 2 条 post（错误 + stderr 开头）
        self.assertGreaterEqual(len(posts), 2)
        self.assertEqual(posts[0]["type"], "log")
        self.assertIn("ffmpeg 返回 1", posts[0]["message"])

    def test_empty_stderr(self):
        posts = []
        proc = MagicMock()
        proc.returncode = 1
        from subtitle_app.muxer import _log_ffmpeg_error
        _log_ffmpeg_error(posts.append, "cmd", proc, "")
        self.assertIn("无错误输出", posts[1]["message"])


if __name__ == "__main__":
    unittest.main()