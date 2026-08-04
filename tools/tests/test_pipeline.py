#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pipeline.py 单元测试（适配当前代码，使用 mock 依赖）"""
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from subtitle_app.pipeline import SubtitleWorker


class TestSubtitleWorkerInit(unittest.TestCase):
    """SubtitleWorker 基本构造"""

    def test_constructor_and_stop_requested(self):
        w = SubtitleWorker()
        self.assertIsNotNone(w)
        self.assertFalse(w.stop_requested)
        self.assertIsNone(w.thread)
        self.assertIsNotNone(w.transcriber)
        w.stop_requested = True
        self.assertTrue(w.stop_requested)
        w.stop_requested = False
        self.assertFalse(w.stop_requested)


class TestSubtitleWorkerProcManagement(unittest.TestCase):
    """子进程注册/注销/终止"""

    def setUp(self):
        self.w = SubtitleWorker()

    def test_register_unregister_and_missing(self):
        proc = MagicMock()
        proc.poll.return_value = None  # 进程仍在运行
        self.w._register_proc(proc)
        self.assertIn(proc, self.w._active_procs)
        self.w._unregister_proc(proc)
        self.assertNotIn(proc, self.w._active_procs)
        # 注销不存在的进程不报错
        self.w._unregister_proc(MagicMock())

    def test_terminate_all_and_timeout_kill(self):
        proc1 = MagicMock()
        proc1.poll.return_value = None
        proc2 = MagicMock()
        proc2.poll.return_value = 0  # 已退出
        self.w._register_proc(proc1)
        self.w._register_proc(proc2)
        self.w._terminate_all_procs()
        proc1.terminate.assert_called_once()
        proc1.wait.assert_called_once_with(timeout=5)
        proc2.terminate.assert_not_called()  # 已退出，不终止
        # terminate 超时 → kill 兜底
        proc = MagicMock()
        proc.poll.return_value = None
        proc.wait.side_effect = [subprocess.TimeoutExpired("cmd", 5), None]
        self.w._register_proc(proc)
        self.w._terminate_all_procs()
        proc.terminate.assert_called_once()
        self.assertEqual(proc.wait.call_count, 2)  # 第一次 wait 超时，第二次是 kill 后 wait
        proc.kill.assert_called_once()


class TestStartAndStop(unittest.TestCase):
    """启动和停止"""

    @patch("subtitle_app.pipeline.Transcriber")
    def test_start_stop_and_unstarted_safety(self, MockTranscriber):
        w = SubtitleWorker()
        w.start([Path("test.mp4")], {"work_dir": "/tmp", "post": MagicMock()})
        self.assertIsNotNone(w.thread)
        self.assertTrue(w.thread.daemon)
        self.assertTrue(w.thread.is_alive())
        # stop 置位并让线程退出
        w.stop()
        self.assertTrue(w.stop_requested)
        w.thread.join(timeout=2)
        self.assertFalse(w.thread.is_alive())
        # 未 start 时 stop 应安全
        w2 = SubtitleWorker()
        try:
            w2.stop()
        except Exception:
            self.fail("stop() on unstarted worker raised exception")


class TestIdxPost(unittest.TestCase):
    """_idx_post 包装器"""

    def setUp(self):
        self.w = SubtitleWorker()

    def test_idx_post_adds_idx_and_passthrough(self):
        post = MagicMock()
        wrapped = self.w._idx_post(post, 3, 10)
        # progress 事件补 idx/total
        wrapped({"type": "progress", "percent": 50})
        post.assert_called_once_with({"type": "progress", "percent": 50, "idx": 3, "total": 10})
        # 非 progress 事件原样透传
        post.reset_mock()
        wrapped({"type": "log", "message": "hello"})
        post.assert_called_once_with({"type": "log", "message": "hello"})


class TestTranscribeStage(unittest.TestCase):
    """_transcribe_stage 函数"""

    def setUp(self):
        self.w = SubtitleWorker()
        self.post = MagicMock()
        self.base_opts = {
            "work_dir": str(Path.cwd()),
            "model_dir": "fake-model",
            "language": "auto",
            "device": "cpu",
            "compute_type": "int8",
            "translate_enabled": False,
            "extract_audio": True,
            "vad_filter": True,
            "api_url": "",
            "api_key": "",
            "translation_model": "",
            "skip_completed": False,
            "post": self.post,
            "_is_stopped": lambda: False,
            "_register_proc": MagicMock(),
            "_unregister_proc": MagicMock(),
        }

    @patch("subtitle_app.pipeline.find_tool")
    def test_transcribe_stage_subtitle_only(self, mock_find_tool):
        """已有字幕文件直接返回"""
        mock_find_tool.return_value = None

        with tempfile.TemporaryDirectory() as d:
            srt_file = Path(d) / "test.srt"
            srt_file.write_text("1\n00:00:01,000 --> 00:00:02,000\nHello\n", encoding="utf-8")

            result = self.w._transcribe_stage(srt_file, 1, 1, self.base_opts)

            self.assertIsNotNone(result)
            self.assertEqual(result["source_srt"], srt_file)
            self.assertEqual(result["detected_lang"], "auto")

    @patch("subtitle_app.pipeline.find_tool")
    @patch("subtitle_app.pipeline.Transcriber")
    def test_transcribe_stage_video(self, MockTranscriber, mock_find_tool):
        """视频文件调用 Transcriber.transcribe_video"""
        mock_find_tool.side_effect = ["/usr/bin/ffmpeg", "/usr/bin/ffprobe"]
        mock_transcriber = MagicMock()
        MockTranscriber.return_value = mock_transcriber
        self.w.transcriber = mock_transcriber

        mock_transcriber.transcribe_video.return_value = (Path("/tmp/output.srt"), "en")

        with tempfile.TemporaryDirectory() as d:
            video = Path(d) / "test.mp4"
            video.write_text("fake video content")

            result = self.w._transcribe_stage(video, 1, 1, self.base_opts)

            self.assertIsNotNone(result)
            mock_transcriber.transcribe_video.assert_called_once()

    @patch("subtitle_app.pipeline.find_tool")
    def test_transcribe_stage_missing_and_unsupported(self, mock_find_tool):
        """不存在的文件与不支持格式都应抛出 RuntimeError"""
        mock_find_tool.return_value = None
        with self.assertRaises(RuntimeError):
            self.w._transcribe_stage(Path("/nonexistent/file.mp4"), 1, 1, self.base_opts)
        with tempfile.TemporaryDirectory() as d:
            bad_file = Path(d) / "test.xyz"
            bad_file.write_text("data", encoding="utf-8")
            with self.assertRaises(RuntimeError) as ctx:
                self.w._transcribe_stage(bad_file, 1, 1, self.base_opts)
            self.assertIn("不支持的文件格式", str(ctx.exception))

    def test_stop_requested_during_transcribe(self):
        """停止请求时应返回 None"""
        self.w.stop_requested = True
        with tempfile.TemporaryDirectory() as d:
            srt = Path(d) / "test.srt"
            srt.write_text("1\n00:00:01,000 --> 00:00:02,000\nHi\n", encoding="utf-8")
            result = self.w._transcribe_stage(srt, 1, 1, self.base_opts)
            self.assertIsNone(result)

    @patch("subtitle_app.pipeline.find_tool")
    def test_transcribe_stage_skip_completed_with_state(self, mock_find_tool):
        """断点续翻：发现 translate_state.json 时读取已有字幕"""
        mock_find_tool.side_effect = ["/usr/bin/ffmpeg", "/usr/bin/ffprobe"]

        with tempfile.TemporaryDirectory() as d:
            item = Path(d) / "test.mp4"
            item.write_text("fake", encoding="utf-8")
            output_dir = Path(d)  # pipeline 中 video 的 output_dir = item.parent
            # 已有的翻译状态
            state = output_dir / "test.translate_state.json"
            state.write_text('{"done": {"0": "hi"}, "updated_at": "2024-01-01"}', encoding="utf-8")
            # 已有的 SRT（源字幕，使用不同后缀以通过过滤条件）
            existing_srt = output_dir / "test.source.srt"
            existing_srt.write_text("1\n00:00:01,000 --> 00:00:02,000\nHi\n", encoding="utf-8")

            opts = {**self.base_opts, "skip_completed": True}
            result = self.w._transcribe_stage(item, 1, 1, opts)

            self.assertIsNotNone(result)
            # 应发现 translate_state 并进入恢复路径
            self.assertEqual(result["source_srt"], existing_srt)


class TestProcessOne(unittest.TestCase):
    """_process_one 串行处理"""

    def setUp(self):
        self.w = SubtitleWorker()
        self.post = MagicMock()
        self.base_opts = {
            "work_dir": str(Path.cwd()),
            "model_dir": "fake-model",
            "language": "auto",
            "target_lang": "zh",
            "device": "cpu",
            "compute_type": "int8",
            "translate_enabled": False,
            "extract_audio": True,
            "vad_filter": True,
            "api_url": "",
            "api_key": "",
            "translation_model": "",
            "skip_completed": False,
            "post": self.post,
            "_is_stopped": lambda: False,
            "_register_proc": MagicMock(),
            "_unregister_proc": MagicMock(),
        }

    @patch("subtitle_app.pipeline.translate_stage")
    @patch("subtitle_app.pipeline.find_tool")
    def test_process_one_calls_both_stages(self, mock_find_tool, mock_translate):
        """_process_one 依次调用转写和翻译"""
        mock_find_tool.return_value = None

        with tempfile.TemporaryDirectory() as d:
            srt = Path(d) / "test.srt"
            srt.write_text("1\n00:00:01,000 --> 00:00:02,000\nHello\n", encoding="utf-8")

            self.w._process_one(srt, 1, 1, self.base_opts)
            mock_translate.assert_called_once()


class TestRun(unittest.TestCase):
    """_run 完整流水线"""

    def setUp(self):
        self.w = SubtitleWorker()
        self.post = MagicMock()

    @patch("subtitle_app.pipeline.translate_stage")
    @patch("subtitle_app.pipeline.find_tool")
    def test_run_serial_and_parallel_complete(self, mock_find_tool, mock_translate):
        """串行（concurrency=1）与并行（concurrency=2）完成后都发出 done 事件"""
        mock_find_tool.return_value = None
        for concurrency in (1, 2):
            with self.subTest(concurrency=concurrency):
                self.post.reset_mock()
                with tempfile.TemporaryDirectory() as d:
                    srt = Path(d) / "test.srt"
                    srt.write_text("1\n00:00:01,000 --> 00:00:02,000\nHello\n", encoding="utf-8")
                    self.w._run([srt], self._make_opts(concurrency))
                done_calls = [c for c in self.post.call_args_list
                              if c[0][0].get("type") == "done"]
                self.assertTrue(len(done_calls) >= 1)


    @patch("subtitle_app.pipeline.translate_stage")
    @patch("subtitle_app.pipeline.find_tool")
    def test_run_parallel_reports_translation_error_after_stream_end(self, mock_find_tool, mock_translate):
        """_STREAM_END 提前结束消费循环时，仍应上报翻译 future 的异常"""
        mock_find_tool.return_value = None
        mock_translate.side_effect = RuntimeError("translation failed")
        opts = self._make_opts(2)

        with tempfile.TemporaryDirectory() as d:
            srt = Path(d) / "test.srt"
            srt.write_text("1\n00:00:01,000 --> 00:00:02,000\nHello\n", encoding="utf-8")

            self.w._run([srt], opts)

        errors = [c[0][0] for c in self.post.call_args_list
                  if c[0][0].get("type") == "error"]
        self.assertTrue(errors, "并行翻译异常应发出 error 事件")
        self.assertIn("translation failed", errors[0]["message"])

    def _make_opts(self, concurrency: int) -> dict:
        """构造 _run 所需的 opts（与现有测试保持一致）"""
        return {
            "work_dir": str(Path.cwd()),
            "model_dir": "fake",
            "language": "auto",
            "device": "cpu",
            "compute_type": "int8",
            "translate_enabled": False,
            "extract_audio": True,
            "vad_filter": True,
            "api_url": "",
            "api_key": "",
            "translation_model": "",
            "skip_completed": False,
            "post": self.post,
            "concurrency": concurrency,
            "_is_stopped": lambda: False,
            "_register_proc": MagicMock(),
            "_unregister_proc": MagicMock(),
        }

    @patch("subtitle_app.pipeline.translate_stage")
    def test_run_stop_is_not_error(self, mock_translate):
        """回归：串行/并行用户停止时，转写线程的「用户停止」异常不得报为 error"""
        for concurrency, files in ((1, ["test.mp4"]), (2, ["test.mp4", "test2.mp4"])):
            with self.subTest(concurrency=concurrency):
                self.w.stop_requested = True
                self.post.reset_mock()
                with patch.object(SubtitleWorker, "_transcribe_stage",
                                  side_effect=RuntimeError("用户停止")):
                    self.w._run([Path(f) for f in files], self._make_opts(concurrency))
                types = [c[0][0].get("type") for c in self.post.call_args_list]
                self.assertNotIn("error", types, "停止不应发出 error 事件")
                self.assertIn("done", types, "停止应发出 done 事件")
                self.w.stop_requested = False

    @patch("subtitle_app.pipeline.translate_stage")
    @patch("subtitle_app.local_service.is_service_running")
    @patch("subtitle_app.local_service.shutdown_owned")
    def test_run_staged_releases_whisper_and_forces_auto_embed(self, mock_shutdown, mock_running, mock_translate):
        """阶段批量：转写后释放 Whisper 显存；本地模式同时清理本会话 llama；
        pause_before_embed 被强制为 False（按用户要求阶段批量自动嵌入）"""
        mock_running.return_value = False
        mock_translate.return_value = None

        self.w._transcribe_stage = lambda item, idx, total, opts: {"path": str(item), "idx": idx, "total": total, "srt": 1}
        releases = []
        self.w.transcriber.release_model = lambda: releases.append(1)

        opts = self._make_opts(concurrency=2)
        opts["api_url"] = "http://127.0.0.1:8080/v1/chat/completions"
        opts["pause_before_embed"] = True

        with tempfile.TemporaryDirectory() as d:
            files = [Path(d) / f"f{i}.mp4" for i in range(2)]
            for p in files:
                p.write_bytes(b"")
            self.w._run(files, opts)

        # 阶段切换释放 Whisper 显存
        self.assertTrue(releases, "进入翻译阶段前应释放 Whisper 显存")
        # 转写阶段前应停掉本会话拉起的翻译服务
        mock_shutdown.assert_called_once()
        # 阶段批量强制自动嵌入：translate_stage 收到的 opts 中 pause_before_embed 应为 False
        self.assertTrue(mock_translate.call_count >= 2)
        for call in mock_translate.call_args_list:
            self.assertFalse(call[0][1]["pause_before_embed"])

    @patch("subtitle_app.pipeline.translate_stage")
    @patch("subtitle_app.local_service.is_service_running")
    @patch("subtitle_app.local_service.shutdown_owned")
    def test_run_staged_warns_on_external_service(self, mock_shutdown, mock_running, mock_translate):
        """阶段 1 前检测到外部 llama-server 在跑时应发 WARNING 提示（不强制杀）"""
        mock_running.return_value = True   # 外部服务在跑
        mock_translate.return_value = None

        self.w._transcribe_stage = lambda item, idx, total, opts: {"path": str(item), "idx": idx, "total": total, "srt": 1}

        opts = self._make_opts(concurrency=2)
        opts["api_url"] = "http://127.0.0.1:8080/v1/chat/completions"

        with tempfile.TemporaryDirectory() as d:
            srt = Path(d) / "a.mp4"
            srt.write_bytes(b"")
            self.w._run([srt], opts)

        warns = [c[0][0] for c in self.post.call_args_list
                 if c[0][0].get("level") == "WARNING"]
        self.assertTrue(warns, "外部翻译服务在跑时应发出 WARNING 提示")


if __name__ == "__main__":
    unittest.main()
