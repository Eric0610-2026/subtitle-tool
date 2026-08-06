#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""muxer.py 单元测试"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

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

    def test_trailing_blank_block_is_sanitized(self):
        """末尾空白文本块必须被净化过滤，不能误判为未变化而返回原文件"""
        from subtitle_app.srt_utils import parse_srt
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "blank_tail.srt"
            # 直接写原始 SRT：第一块正常，末尾块文本为空白行（strip 后为空）
            p.write_text(
                "1\n00:00:01,000 --> 00:00:02,000\nHello\n"
                "\n"
                "2\n00:00:03,000 --> 00:00:04,000\n   \n",
                encoding="utf-8",
            )
            from subtitle_app.muxer import _sanitize_srt_for_mux
            result = _sanitize_srt_for_mux(p)
            self.assertNotEqual(result, p)  # 必须走净化写出，而不是返回原文件
            cleaned = parse_srt(result)
            self.assertEqual(len(cleaned), 1)
            self.assertEqual(cleaned[0].text, "Hello")
            self.assertEqual(cleaned[0].start, 1.0)
            self.assertEqual(cleaned[0].end, 2.0)

    def test_nonexistent_file_returns_same_path(self):
        """不存在的文件应返回原路径（在 parse_srt 中抛出异常）"""
        from subtitle_app.muxer import _sanitize_srt_for_mux
        result = _sanitize_srt_for_mux(Path("nonexistent.srt"))
        self.assertEqual(result, Path("nonexistent.srt"))


class TestFindSiblingProbe(unittest.TestCase):
    """_find_sibling_probe"""

    def test_finds_sibling_or_none(self):
        with tempfile.TemporaryDirectory() as d:
            from subtitle_app.muxer import _find_sibling_probe
            # 子目录隔离各场景，避免同名 probe 干扰
            exe = Path(d) / "exe"; exe.mkdir()
            (exe / "ffmpeg.exe").write_text("dummy")
            (exe / "ffprobe.exe").write_text("dummy")
            self.assertEqual(_find_sibling_probe(str(exe / "ffmpeg.exe")),
                             str(exe / "ffprobe.exe"))
            bare = Path(d) / "bare"; bare.mkdir()
            (bare / "ffmpeg").write_text("dummy")
            (bare / "ffprobe").write_text("dummy")
            self.assertEqual(_find_sibling_probe(str(bare / "ffmpeg")),
                             str(bare / "ffprobe"))
            solo = Path(d) / "solo"; solo.mkdir()
            (solo / "ffmpeg2.exe").write_text("dummy")
            self.assertIsNone(_find_sibling_probe(str(solo / "ffmpeg2.exe")))


class TestCountExistingSubStreams(unittest.TestCase):
    """_count_existing_sub_streams"""

    @patch("subtitle_app.muxer.subprocess.run")
    def test_counts_streams(self, mock_run):
        from subtitle_app.muxer import _count_existing_sub_streams
        # 正常计数
        mock_run.return_value = MagicMock(returncode=0, stdout="0\n1\n2\n")
        self.assertEqual(
            _count_existing_sub_streams(Path("test.mp4"), "ffprobe.exe"), 3)
        # 无 ffprobe → 0 且不调用
        mock_run.reset_mock()
        self.assertEqual(
            _count_existing_sub_streams(Path("test.mp4"), None), 0)
        mock_run.assert_not_called()
        # 调用异常 → 0
        mock_run.side_effect = FileNotFoundError
        self.assertEqual(
            _count_existing_sub_streams(Path("test.mp4"), "ffprobe.exe"), 0)


class TestProbeDuration(unittest.TestCase):
    """_probe_duration"""

    @patch("subtitle_app.muxer.subprocess.run")
    def test_probes_duration(self, mock_run):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "test.mp4"
            p.write_text("dummy")
            from subtitle_app.muxer import _probe_duration
            # 正常返回 Duration
            mock_run.return_value = MagicMock(returncode=0, stdout="123.456\n")
            self.assertAlmostEqual(_probe_duration(p, "ffprobe.exe"), 123.456)
            # 无 ffprobe → None 且不调用
            mock_run.reset_mock()
            self.assertIsNone(_probe_duration(p, None))
            mock_run.assert_not_called()
            # N/A 输出 → None
            mock_run.return_value = MagicMock(returncode=0, stdout="N/A\n")
            self.assertIsNone(_probe_duration(p, "ffprobe.exe"))
            # 不存在文件 → None
            mock_run.reset_mock()
            self.assertIsNone(_probe_duration(Path("nonexistent.mp4"), "ffprobe.exe"))
            # 调用异常 → None
            mock_run.side_effect = FileNotFoundError
            self.assertIsNone(_probe_duration(p, "ffprobe.exe"))


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

    def test_builds_embed_command(self):
        with tempfile.TemporaryDirectory() as d:
            video = Path(d) / "test.mp4"
            srt = Path(d) / "test.srt"
            mkv = Path(d) / "test.mkv"
            from subtitle_app.muxer import _build_embed_cmd
            cmd = _build_embed_cmd("ffmpeg.exe", video, srt, mkv)
        # 基础结构
        self.assertEqual(cmd[0], "ffmpeg.exe")
        self.assertIn("-y", cmd)
        self.assertIn(str(video), cmd)
        self.assertIn(str(srt), cmd)
        self.assertIn(str(mkv), cmd)
        self.assertIn("-c:v", cmd)
        self.assertIn("copy", cmd)
        # map 参数：视频/音频/字幕三个输入
        idx_v = cmd.index("-map")
        self.assertEqual(cmd[idx_v + 1], "0:v?")
        self.assertEqual(cmd[idx_v + 3], "0:a?")
        self.assertEqual(cmd[idx_v + 5], "1")


class TestCleanupFile(unittest.TestCase):
    """_cleanup_file"""

    def test_cleanup_noop_and_removes(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "test.tmp"
            from subtitle_app.muxer import _cleanup_file
            # None：不抛异常
            _cleanup_file(None)
            # 不存在：不抛异常
            _cleanup_file(p)
            # 存在：删除
            p.write_text("dummy")
            self.assertTrue(p.exists())
            _cleanup_file(p)
            self.assertFalse(p.exists())


class TestLogFfmpegError(unittest.TestCase):
    """_log_ffmpeg_error"""

    def test_posts_error(self):
        proc = MagicMock()
        proc.returncode = 1
        from subtitle_app.muxer import _log_ffmpeg_error
        # 有 stderr 内容
        posts = []
        _log_ffmpeg_error(posts.append, "ffmpeg -i test.mp4", proc,
                          "error: something went wrong")
        self.assertGreaterEqual(len(posts), 2)
        self.assertEqual(posts[0]["type"], "log")
        self.assertIn("ffmpeg 返回 1", posts[0]["message"])
        # 空 stderr
        posts = []
        _log_ffmpeg_error(posts.append, "cmd", proc, "")
        self.assertIn("无错误输出", posts[1]["message"])


class TestEmbedSubtitlesMissingFiles(unittest.TestCase):
    """embed_subtitles_to_video 缺文件时必须返回 (None, False)，避免解包崩溃"""

    def test_missing_input_returns_empty_tuple(self):
        from subtitle_app.muxer import embed_subtitles_to_video
        posts = []
        # 缺视频
        with tempfile.TemporaryDirectory() as d:
            srt = Path(d) / "a.srt"
            srt.write_text("1\n00:00:00,000 --> 00:00:01,000\nHi\n", encoding="utf-8")
            result = embed_subtitles_to_video(Path(d) / "missing.mp4", srt,
                                              "ffmpeg.exe", posts.append)
            self.assertEqual(result, (None, False))
        # 缺字幕
        with tempfile.TemporaryDirectory() as d:
            video = Path(d) / "a.mp4"
            video.write_bytes(b"dummy")
            result = embed_subtitles_to_video(video, Path(d) / "missing.srt",
                                              "ffmpeg.exe", posts.append)
            self.assertEqual(result, (None, False))


class TestProbeFirstSubStream(unittest.TestCase):
    """_probe_first_sub_stream"""

    @patch("subtitle_app.muxer.subprocess.run")
    def test_probes_first_sub_stream(self, mock_run):
        from subtitle_app.muxer import _probe_first_sub_stream
        # 无 ffprobe → None 且不调用
        self.assertIsNone(_probe_first_sub_stream(None, Path("test.mkv")))
        mock_run.assert_not_called()
        # 正常返回
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout='{"streams": [{"index": 1, "codec_name": "subrip", '
                   '"tags": {"language": "eng"}}]}')
        info = _probe_first_sub_stream("ffprobe.exe", Path("test.mkv"))
        self.assertEqual(info["index"], 1)
        self.assertEqual(info["codec_name"], "subrip")
        self.assertEqual(info["language"], "eng")
        # 无字幕流 → None
        mock_run.return_value = MagicMock(returncode=0, stdout='{"streams": []}')
        self.assertIsNone(_probe_first_sub_stream("ffprobe.exe", Path("test.mkv")))
        # 调用异常 → None
        mock_run.side_effect = FileNotFoundError
        self.assertIsNone(_probe_first_sub_stream("ffprobe.exe", Path("test.mkv")))


class TestExtractEmbeddedSubtitle(unittest.TestCase):
    """extract_embedded_subtitle"""

    def setUp(self):
        self._sibling_patcher = patch("subtitle_app.muxer._find_sibling_probe")
        self.mock_sibling = self._sibling_patcher.start()
        self.mock_sibling.return_value = "ffprobe.exe"
        self._probe_patcher = patch("subtitle_app.muxer._probe_first_sub_stream")
        self.mock_probe = self._probe_patcher.start()
        self._run_patcher = patch("subtitle_app.muxer._run_ffmpeg")
        self.mock_run = self._run_patcher.start()

    def tearDown(self):
        self._probe_patcher.stop()
        self._sibling_patcher.stop()
        self._run_patcher.stop()

    def _video(self, d):
        v = Path(d) / "movie.mkv"
        v.write_bytes(b"dummy")
        return v

    def _mock_success(self, cmd, post, timeout, register_proc=None, unregister_proc=None,
                      timeout_msg=None):
        """模拟 ffmpeg 成功：创建输出 SRT 文件"""
        out = Path(cmd[-1])
        out.write_text("1\n00:00:01,000 --> 00:00:02,000\nHello\n", encoding="utf-8")
        proc = MagicMock()
        proc.returncode = 0
        return proc, "", ""

    def _mock_failure(self, cmd, post, timeout, register_proc=None, unregister_proc=None,
                      timeout_msg=None):
        """模拟 ffmpeg 失败：不创建输出文件"""
        proc = MagicMock()
        proc.returncode = 1
        return proc, "", "some error"

    def test_missing_file(self):
        from subtitle_app.muxer import extract_embedded_subtitle
        posts = []
        srt, status = extract_embedded_subtitle(Path("missing.mkv"), "ffmpeg.exe", posts.append)
        self.assertIsNone(srt)
        self.assertIn("不存在", status)
        self.mock_run.assert_not_called()

    def test_image_subtitle_refused(self):
        """图像字幕（如 hdmv_pgs_subtitle）必须拒绝，不能尝试转 SRT"""
        self.mock_probe.return_value = {"index": 1, "codec_name": "hdmv_pgs_subtitle", "language": "eng"}
        with tempfile.TemporaryDirectory() as d:
            v = self._video(d)
            from subtitle_app.muxer import extract_embedded_subtitle
            posts = []
            srt, status = extract_embedded_subtitle(v, "ffmpeg.exe", posts.append)
        self.assertIsNone(srt)
        self.assertIn("图像字幕", status)
        self.mock_run.assert_not_called()

    def test_successful_extract(self):
        """文本字幕流 → ffmpeg 成功 → 返回 SRT 路径"""
        self.mock_probe.return_value = {"index": 0, "codec_name": "subrip", "language": "eng"}
        self.mock_run.side_effect = self._mock_success
        with tempfile.TemporaryDirectory() as d:
            v = self._video(d)
            from subtitle_app.muxer import extract_embedded_subtitle
            posts = []
            srt, status = extract_embedded_subtitle(v, "ffmpeg.exe", posts.append)
            out = Path(d) / "movie.srt"
            self.assertEqual(srt, out)
            self.assertTrue(out.exists())
            self.assertIn("提取完成", status)
            # ffmpeg 命令应包含 -map 0:s:0 与 srt 编码
            cmd = self.mock_run.call_args[0][0]
            self.assertIn("-map", cmd)
            self.assertIn("0:s:0", cmd)
            self.assertIn("-c:s", cmd)
            self.assertIn("srt", cmd)
            self.assertIn(str(out), cmd)

    def test_conflict_name_appends_suffix(self):
        """同名 SRT 已存在 → 使用 _extracted1.srt 避免覆盖"""
        self.mock_probe.return_value = {"index": 0, "codec_name": "srt", "language": ""}
        self.mock_run.side_effect = self._mock_success
        with tempfile.TemporaryDirectory() as d:
            v = self._video(d)
            (Path(d) / "movie.srt").write_text("old", encoding="utf-8")
            from subtitle_app.muxer import extract_embedded_subtitle
            posts = []
            srt, _ = extract_embedded_subtitle(v, "ffmpeg.exe", posts.append)
            self.assertEqual(srt, Path(d) / "movie_extracted1.srt")
            self.assertTrue((Path(d) / "movie_extracted1.srt").exists())
            # 已有文件未被覆盖
            self.assertEqual((Path(d) / "movie.srt").read_text(encoding="utf-8"), "old")

    def test_conflict_name_second_extract_uses_next_index(self):
        """movie_extracted1.srt 也已存在 → 使用 _extracted2.srt，绝不静默覆盖"""
        self.mock_probe.return_value = {"index": 0, "codec_name": "srt", "language": ""}
        self.mock_run.side_effect = self._mock_success
        with tempfile.TemporaryDirectory() as d:
            v = self._video(d)
            (Path(d) / "movie.srt").write_text("old", encoding="utf-8")
            (Path(d) / "movie_extracted1.srt").write_text("old1", encoding="utf-8")
            from subtitle_app.muxer import extract_embedded_subtitle
            posts = []
            srt, _ = extract_embedded_subtitle(v, "ffmpeg.exe", posts.append)
            self.assertEqual(srt, Path(d) / "movie_extracted2.srt")
            self.assertEqual((Path(d) / "movie_extracted1.srt").read_text(encoding="utf-8"), "old1")

    def test_ffmpeg_failure_cleans_partial(self):
        """ffmpeg 失败且无输出 → 清理可能的部分文件，返回 None"""
        self.mock_probe.return_value = {"index": 0, "codec_name": "subrip", "language": ""}
        self.mock_run.side_effect = self._mock_failure
        with tempfile.TemporaryDirectory() as d:
            v = self._video(d)
            from subtitle_app.muxer import extract_embedded_subtitle
            posts = []
            srt, status = extract_embedded_subtitle(v, "ffmpeg.exe", posts.append)
            self.assertIsNone(srt)
            self.assertIn("失败", status)
            self.assertFalse((Path(d) / "movie.srt").exists())

    def test_no_probe_info_still_attempts(self):
        """探测无结果（无 ffprobe）→ 仍尝试直接提取"""
        self.mock_probe.return_value = None
        self.mock_run.side_effect = self._mock_success
        with tempfile.TemporaryDirectory() as d:
            v = self._video(d)
            from subtitle_app.muxer import extract_embedded_subtitle
            posts = []
            srt, _ = extract_embedded_subtitle(v, "ffmpeg.exe", posts.append)
            self.assertEqual(srt, Path(d) / "movie.srt")


class TestConvertToMp4(unittest.TestCase):
    """convert_to_mp4 及辅助函数"""

    def _video(self, d, name="movie.mkv"):
        v = Path(d) / name
        v.write_bytes(b"dummy")
        return v

    def _mock_success(self, cmd, post, timeout, register_proc=None, unregister_proc=None,
                      timeout_msg=None):
        """模拟 ffmpeg 成功：创建输出 MP4 文件（需大于 _MP4_MIN_SIZE）"""
        out = Path(cmd[-1])
        out.write_bytes(b"x" * 2048)
        proc = MagicMock()
        proc.returncode = 0
        return proc, "", ""

    def _mock_failure(self, cmd, post, timeout, register_proc=None, unregister_proc=None,
                      timeout_msg=None):
        """模拟 ffmpeg 失败：不创建输出文件"""
        proc = MagicMock()
        proc.returncode = 1
        return proc, "", "some error"

    def test_next_mp4_path(self):
        """_next_mp4_path：优先同名，已存在则 _convertedN"""
        with tempfile.TemporaryDirectory() as d:
            v = self._video(d)
            from subtitle_app.muxer import _next_mp4_path
            self.assertEqual(_next_mp4_path(v), Path(d) / "movie.mp4")
            (Path(d) / "movie.mp4").write_bytes(b"x")
            self.assertEqual(_next_mp4_path(v), Path(d) / "movie_converted1.mp4")
            (Path(d) / "movie_converted1.mp4").write_bytes(b"x")
            self.assertEqual(_next_mp4_path(v), Path(d) / "movie_converted2.mp4")

    def test_build_mp4_cmd_excludes_subs(self):
        """_build_mp4_cmd：默认 copy，且 -sn 排除字幕流"""
        from subtitle_app.muxer import _build_mp4_cmd
        cmd = _build_mp4_cmd("ffmpeg.exe", Path("a.mkv"), Path("a.mp4"))
        self.assertIn("-sn", cmd)
        self.assertIn("-c:v", cmd)
        self.assertIn("copy", cmd)
        self.assertIn("-movflags", cmd)
        self.assertIn("+faststart", cmd)
        # 不应出现字幕映射
        self.assertNotIn("0:s", cmd)
        # 降级参数生效
        cmd2 = _build_mp4_cmd("ffmpeg.exe", Path("a.mkv"), Path("a.mp4"),
                              video_codec="libx264", audio_codec="aac")
        self.assertIn("libx264", cmd2)
        self.assertIn("aac", cmd2)

    def test_success_stream_copy(self):
        """流复制成功 + 时长验证通过 → (mp4, True)"""
        with tempfile.TemporaryDirectory() as d:
            v = self._video(d)
            from subtitle_app.muxer import convert_to_mp4
            with patch("subtitle_app.muxer._run_ffmpeg", side_effect=self._mock_success) as mock_run, \
                 patch("subtitle_app.muxer._verify_duration", return_value=(True, "ok")) as mock_verify:
                posts = []
                mp4, trustworthy = convert_to_mp4(v, "ffmpeg.exe", posts.append)
            self.assertEqual(mp4, Path(d) / "movie.mp4")
            self.assertTrue(mp4.exists())
            self.assertTrue(trustworthy)
            self.assertEqual(mock_run.call_count, 1)  # 首次即成功，不降级
            mock_verify.assert_called_once()

    def test_fallback_audio_reencode(self):
        """流复制失败 → 降级音频重编码 → 成功"""
        with tempfile.TemporaryDirectory() as d:
            v = self._video(d)
            from subtitle_app.muxer import convert_to_mp4
            calls = {"n": 0}
            def flaky(cmd, post, timeout, register_proc=None, unregister_proc=None,
                      timeout_msg=None):
                calls["n"] += 1
                if calls["n"] == 1:
                    proc = MagicMock()
                    proc.returncode = 1
                    return proc, "", "copy failed"
                return self._mock_success(cmd, post, timeout)
            with patch("subtitle_app.muxer._run_ffmpeg", side_effect=flaky) as mock_run, \
                 patch("subtitle_app.muxer._verify_duration", return_value=(True, "ok")):
                posts = []
                mp4, trustworthy = convert_to_mp4(v, "ffmpeg.exe", posts.append)
            self.assertEqual(mp4, Path(d) / "movie.mp4")
            self.assertTrue(trustworthy)
            self.assertEqual(mock_run.call_count, 2)
            # 第二次尝试应使用 aac 音频
            cmd2 = mock_run.call_args_list[1][0][0]
            self.assertIn("aac", cmd2)

    def test_all_attempts_fail(self):
        """三轮全失败 → (None, False) 且无残留文件"""
        with tempfile.TemporaryDirectory() as d:
            v = self._video(d)
            from subtitle_app.muxer import convert_to_mp4
            with patch("subtitle_app.muxer._run_ffmpeg", side_effect=self._mock_failure):
                posts = []
                mp4, trustworthy = convert_to_mp4(v, "ffmpeg.exe", posts.append)
            self.assertIsNone(mp4)
            self.assertFalse(trustworthy)
            self.assertFalse((Path(d) / "movie.mp4").exists())
            self.assertFalse((Path(d) / "movie_converted1.mp4").exists())

    def test_duration_verify_fail_keeps_file(self):
        """转换成功但时长验证失败 → 继续降级尝试，最终保留 mp4 但 is_trustworthy=False"""
        with tempfile.TemporaryDirectory() as d:
            v = self._video(d)
            from subtitle_app.muxer import convert_to_mp4
            calls = {"n": 0}
            def succeed_after_verify(cmd, post, timeout, register_proc=None, unregister_proc=None,
                                     timeout_msg=None):
                calls["n"] += 1
                return self._mock_success(cmd, post, timeout)
            # 第一级：验证失败；第二级：验证通过
            verify_results = [False, True]
            def verify_side(v, m, ff):
                return verify_results.pop(0), "detail"
            with patch("subtitle_app.muxer._run_ffmpeg", side_effect=succeed_after_verify) as mock_run, \
                 patch("subtitle_app.muxer._verify_duration", side_effect=verify_side):
                posts = []
                mp4, trustworthy = convert_to_mp4(v, "ffmpeg.exe", posts.append)
            self.assertEqual(mp4, Path(d) / "movie.mp4")
            self.assertTrue(trustworthy)  # 第二级重编码修复了时长问题
            self.assertEqual(mock_run.call_count, 2)

    def test_duration_verify_fail_all_levels(self):
        """所有级别时长验证都失败 → 保留 mp4 候选但 is_trustworthy=False"""
        with tempfile.TemporaryDirectory() as d:
            v = self._video(d)
            from subtitle_app.muxer import convert_to_mp4
            with patch("subtitle_app.muxer._run_ffmpeg", side_effect=self._mock_success), \
                 patch("subtitle_app.muxer._verify_duration", return_value=(False, "时长不符")):
                posts = []
                mp4, trustworthy = convert_to_mp4(v, "ffmpeg.exe", posts.append)
            self.assertEqual(mp4, Path(d) / "movie.mp4")
            self.assertFalse(trustworthy)

    def test_mp4_without_subs_skips(self):
        """源已是 MP4 且无内嵌字幕 → 跳过，不调用 ffmpeg，原文件不变"""
        with tempfile.TemporaryDirectory() as d:
            v = Path(d) / "movie.mp4"
            v.write_bytes(b"original")
            from subtitle_app.muxer import convert_to_mp4
            with patch("subtitle_app.muxer._count_existing_sub_streams", return_value=0), \
                 patch("subtitle_app.muxer._run_ffmpeg") as mock_run:
                posts = []
                mp4, trustworthy = convert_to_mp4(v, "ffmpeg.exe", posts.append)
            self.assertEqual(mp4, v)
            self.assertTrue(trustworthy)
            self.assertEqual(v.read_bytes(), b"original")
            mock_run.assert_not_called()

    def test_mp4_with_subs_stripped(self):
        """源已是 MP4 且带内嵌字幕 → 重封装去字幕并原子替换原文件"""
        with tempfile.TemporaryDirectory() as d:
            v = Path(d) / "movie.mp4"
            v.write_bytes(b"original")
            from subtitle_app.muxer import convert_to_mp4
            with patch("subtitle_app.muxer._count_existing_sub_streams", return_value=1), \
                 patch("subtitle_app.muxer._run_ffmpeg", side_effect=self._mock_success) as mock_run, \
                 patch("subtitle_app.muxer._verify_duration", return_value=(True, "ok")) as mock_verify:
                posts = []
                mp4, trustworthy = convert_to_mp4(v, "ffmpeg.exe", posts.append)
            self.assertEqual(mp4, v)
            self.assertTrue(trustworthy)
            # 原文件已被无字幕版本原子替换（内容为模拟输出）
            self.assertEqual(v.read_bytes(), b"x" * 2048)
            mock_run.assert_called_once()
            mock_verify.assert_called_once()
            # 输出应写临时文件再替换，而非直接写目标路径
            self.assertIn(".tmp.mp4", str(mock_run.call_args[0][0][-1]))
            # 无临时文件残留
            self.assertEqual(list(Path(d).glob("*.tmp.mp4")), [])

    def test_mp4_strip_failure_keeps_original(self):
        """源 MP4 去字幕失败 → (None, False)，原文件保留"""
        with tempfile.TemporaryDirectory() as d:
            v = Path(d) / "movie.mp4"
            v.write_bytes(b"original")
            from subtitle_app.muxer import convert_to_mp4
            with patch("subtitle_app.muxer._count_existing_sub_streams", return_value=1), \
                 patch("subtitle_app.muxer._run_ffmpeg", side_effect=self._mock_failure):
                posts = []
                mp4, trustworthy = convert_to_mp4(v, "ffmpeg.exe", posts.append)
            self.assertIsNone(mp4)
            self.assertFalse(trustworthy)
            self.assertEqual(v.read_bytes(), b"original")
            self.assertEqual(list(Path(d).glob("*.tmp.mp4")), [])

    def test_best_file_cleaned_by_later_timeout(self):
        """第一级验证失败记录 best，后续级别超时清理文件 → 返回 (None, False) 而非坏候选"""
        with tempfile.TemporaryDirectory() as d:
            v = self._video(d)
            from subtitle_app.muxer import convert_to_mp4
            calls = {"n": 0}
            def level1_success_then_timeouts(cmd, post, timeout, register_proc=None,
                                             unregister_proc=None, timeout_msg=None):
                calls["n"] += 1
                if calls["n"] == 1:
                    return self._mock_success(cmd, post, timeout)
                return None  # 后续级别超时
            with patch("subtitle_app.muxer._run_ffmpeg", side_effect=level1_success_then_timeouts), \
                 patch("subtitle_app.muxer._verify_duration", return_value=(False, "时长不符")):
                posts = []
                mp4, trustworthy = convert_to_mp4(v, "ffmpeg.exe", posts.append)
            self.assertIsNone(mp4)
            self.assertFalse(trustworthy)
            self.assertFalse((Path(d) / "movie.mp4").exists())

    def test_missing_file(self):
        """文件不存在 → (None, False)"""
        from subtitle_app.muxer import convert_to_mp4
        mp4, trustworthy = convert_to_mp4(Path("missing.mkv"), "ffmpeg.exe", [].append)
        self.assertIsNone(mp4)
        self.assertFalse(trustworthy)


if __name__ == "__main__":
    unittest.main()