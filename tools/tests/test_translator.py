#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""translator.py 单元测试"""
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from subtitle_app.srt_utils import SubtitleBlock


def setUpModule():
    # 所有真实 translate_only 调用均隔离生产备份（包括单文件运行）。
    directory = tempfile.TemporaryDirectory(prefix="subtitle_test_backup_")
    unittest.addModuleCleanup(directory.cleanup)
    backup_patch = patch("subtitle_app.translator._BACKUP_DIR", Path(directory.name))
    backup_patch.start()
    unittest.addModuleCleanup(backup_patch.stop)


def _make_block(index=1, start=1.0, end=3.0, text="Hello world"):
    return SubtitleBlock(index=index, start=start, end=end, text=text)


def _make_srt(path: Path, blocks=None):
    """写入一个简单的 SRT 文件供测试"""
    if blocks is None:
        blocks = [_make_block(1, 1.0, 3.0, "Hello world"),
                  _make_block(2, 4.0, 6.0, "This is a test")]
    from subtitle_app.srt_utils import write_srt
    write_srt(path, blocks, [b.text for b in blocks])
    return path


class TestTranslateStage(unittest.TestCase):
    """translate_stage 入口函数"""

    def test_translate_stage_calls_translate_only(self):
        with tempfile.TemporaryDirectory() as d:
            # 准备一个 SRT 文件
            srt_path = Path(d) / "test.srt"
            _make_srt(srt_path)
            item = Path(d) / "test.mp4"
            item.write_text("dummy")

            result = {
                "source_srt": srt_path,
                "output_dir": Path(d),
                "item": item,
                "idx": 0, "total": 1,
                "detected_lang": "en",
                "_ffmpeg": "ffmpeg.exe",
            }
            opts = {"work_dir": d, "language": "en"}

            posts = []
            with patch("subtitle_app.translator.translate_only") as mock_to:
                from subtitle_app.translator import translate_stage
                translate_stage(result, opts, posts.append)

            # 验证 post 调用了 status
            self.assertEqual(len(posts), 1)
            self.assertEqual(posts[0]["type"], "translate_status")
            self.assertEqual(posts[0]["file"], "test.mp4")

            # 验证 translate_only 被调用
            mock_to.assert_called_once()
            args, kwargs = mock_to.call_args
            self.assertEqual(args[0], srt_path)  # source_srt
            self.assertEqual(args[1], Path(d))   # output_dir
            self.assertEqual(args[2], item)      # item
            self.assertEqual(args[3], 0)         # idx
            self.assertEqual(args[4], 1)         # total
            # opts 合并了 _detected_lang 和 _ffmpeg
            merged_opts = args[5]
            self.assertEqual(merged_opts["language"], "en")
            self.assertEqual(merged_opts["_detected_lang"], "en")
            self.assertEqual(merged_opts["_ffmpeg"], "ffmpeg.exe")


class TestEmbedCancellation(unittest.TestCase):
    def setUp(self):
        from subtitle_app.pipeline import SubtitleWorker
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.source = _make_srt(self.root / "movie.en.srt")
        self.media = self.root / "movie.mp4"
        self.media.write_bytes(b"original video")
        self.worker = SubtitleWorker()
        self.opts = {"work_dir": str(self.root), "language": "en", "translate_enabled": False,
                     "_ffmpeg": "mock-ffmpeg", "pause_before_embed": True,
                     "_is_stopped": lambda: self.worker.stop_requested}
        self.posts = []

    def run_translation(self, post=None):
        from subtitle_app.translator import translate_only
        translate_only(self.source, self.root, self.media, 1, 1, self.opts, post or self.posts.append)

    def assert_cancelled_files_preserved(self, embed):
        embed.assert_not_called()
        self.assertEqual(self.media.read_bytes(), b"original video")
        self.assertTrue(self.source.exists())
        self.assertFalse((self.root / "movie.srt").exists())
        self.assertFalse(any(e["type"] == "output_path" for e in self.posts))
        from subtitle_app.srt_utils import IGNORE_FILE
        self.assertFalse((self.root / IGNORE_FILE).exists())

    def test_worker_stop_interrupts_pending_confirmation(self):
        from subtitle_app.translator import PauseResponse
        entered = threading.Event()
        response = PauseResponse()
        errors = []
        def post(event):
            self.posts.append(event)
            if event["type"] == "pause_before_embed":
                entered.set()
        def run():
            try:
                self.run_translation(post)
            except Exception as exc:
                errors.append(exc)
        with patch("subtitle_app.translator.PauseResponse", return_value=response), \
                patch("subtitle_app.translator.embed_subtitles_to_video") as embed:
            self.worker.thread = threading.Thread(target=run, daemon=True)
            self.worker.thread.start()
            try:
                self.assertTrue(entered.wait(2))
                self.worker.stop()
                self.worker.thread.join(timeout=1)
                self.assertFalse(self.worker.thread.is_alive())
                self.assertEqual(errors, [])
                self.assertTrue(response.cancelled)
                self.assertTrue(response.event.is_set())
                self.assert_cancelled_files_preserved(embed)
            finally:
                self.worker.stop_requested = True
                response.event.set()
                self.worker.thread.join(timeout=2)

    def test_stop_wins_over_simultaneous_confirmation_and_timeout(self):
        from subtitle_app.translator import PauseResponse
        for confirmed in (False, True):
            with self.subTest(confirmed=confirmed):
                self.worker.stop_requested = False
                response = PauseResponse()
                response.event = MagicMock()
                def wait(timeout):
                    self.assertLessEqual(timeout, 0.2)
                    self.worker.stop_requested = True
                    return confirmed
                response.event.wait.side_effect = wait
                with patch("subtitle_app.translator.PauseResponse", return_value=response), \
                        patch("subtitle_app.translator.embed_subtitles_to_video") as embed:
                    self.run_translation()
                    self.assertTrue(response.cancelled)
                    self.assert_cancelled_files_preserved(embed)

    def test_confirmation_skip_and_timeout_keep_existing_behavior(self):
        from subtitle_app.translator import PauseResponse
        for action in ("embed", "skip", "timeout"):
            with self.subTest(action=action):
                response = PauseResponse()
                response.event = MagicMock()
                response.event.wait.return_value = action != "timeout"
                if action == "skip":
                    response.action = "skip"
                with patch("subtitle_app.translator.PauseResponse", return_value=response), \
                        patch("subtitle_app.translator.time.monotonic", side_effect=[0, 3600]), \
                        patch("subtitle_app.translator.embed_subtitles_to_video", return_value=(None, False)) as embed:
                    self.run_translation()
                    self.assertEqual(embed.call_count, 0 if action == "skip" else 1)
                self.assertFalse(response.cancelled)
                self.assertTrue((self.root / "movie.srt").exists())
                self.assertEqual(self.media.read_bytes(), b"original video")

    def test_stop_during_embed_or_before_cleanup_preserves_sources(self):
        for stop_at in ("embed", "output", "cleanup"):
            with self.subTest(stop_at=stop_at):
                self.worker.stop_requested = False
                self.posts.clear()
                self.opts["pause_before_embed"] = False
                mkv = self.root / "movie.mkv"
                mkv.write_bytes(b"trusted test output")
                def embed(*args, **kwargs):
                    if stop_at == "embed":
                        self.worker.stop_requested = True
                    return mkv, True
                def post(event):
                    self.posts.append(event)
                    if stop_at == "output" and event["type"] == "output_path":
                        self.worker.stop_requested = True
                    if stop_at == "cleanup" and "翻译临时字幕已删除" in event.get("message", ""):
                        self.worker.stop_requested = True
                with patch("subtitle_app.translator.embed_subtitles_to_video", side_effect=embed):
                    self.run_translation(post)
                self.assertTrue(self.worker.stop_requested)
                self.assertEqual(self.media.read_bytes(), b"original video")
                self.assertTrue(self.source.exists())
                from subtitle_app.srt_utils import IGNORE_FILE
                self.assertFalse((self.root / IGNORE_FILE).exists())


class TestTranslateOnlyEarlyReturn(unittest.TestCase):
    """translate_only 的早期返回路径"""

    def test_early_returns(self):
        """两个 is_stopped 检查点：translate 前直接返回 / parse 后返回"""
        from subtitle_app.translator import translate_only
        # 第一个检查点为真 → 直接返回，无 post
        with tempfile.TemporaryDirectory() as d:
            srt_path = Path(d) / "test.srt"
            _make_srt(srt_path)
            item = Path(d) / "test.mp4"
            item.write_text("dummy")
            opts = {"work_dir": d, "language": "en", "translate_enabled": False,
                    "_is_stopped": MagicMock(return_value=True)}
            posts = []
            translate_only(srt_path, Path(d), item, 0, 1, opts, posts.append)
            self.assertEqual(len(posts), 0)
        # 第一个为假、parse 后第二个为真 → 只有 log
        with tempfile.TemporaryDirectory() as d:
            srt_path = Path(d) / "test.srt"
            _make_srt(srt_path)
            item = Path(d) / "test.mp4"
            item.write_text("dummy")
            opts = {"work_dir": d, "language": "en", "translate_enabled": False,
                    "_is_stopped": MagicMock(side_effect=[False, True])}
            posts = []
            translate_only(srt_path, Path(d), item, 0, 1, opts, posts.append)
            self.assertGreater(len(posts), 0)
            self.assertEqual(posts[0]["type"], "log")


class TestTranslateOnlyPassthrough(unittest.TestCase):
    """translate_only 不翻译（翻译禁用）"""

    @patch("subtitle_app.translator._prune_backups")
    def test_backup_retention_uses_task_snapshot(self, prune_backups):
        """备份份数来自任务参数，而非运行中可变化的全局配置。"""
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            srt_path = _make_srt(d / "test.srt")
            item = d / "test.mp3"
            item.write_text("dummy")

            from subtitle_app.translator import translate_only
            translate_only(srt_path, d, item, 0, 1, {
                "work_dir": str(d), "language": "en", "translate_enabled": False,
                "backup_max_files": 7,
            }, lambda event: None)

            self.assertEqual(prune_backups.call_count, 2)
            self.assertTrue(all(call.args[1] == 7 for call in prune_backups.call_args_list))


class TestTranslateOnlyWithTranslation(unittest.TestCase):
    """translate_only 翻译路径"""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.d = Path(self.tempdir.name)

    def tearDown(self):
        self.tempdir.cleanup()

    def _run(self, srt_texts=None, detected_lang="en", translation_only=False,
             is_chinese_source=False, mkv_ok=None):
        """辅助：执行 translate_only 并返回 posts"""
        srt_path = self.d / "test.srt"
        blocks = [SubtitleBlock(index=i + 1, start=float(i * 3), end=float(i * 3 + 2),
                                text=t) for i, t in enumerate(srt_texts or ["Hello world", "Test"])]
        from subtitle_app.srt_utils import write_srt
        write_srt(srt_path, blocks, [b.text for b in blocks])

        item = self.d / "test.mp4"
        item.write_text("dummy")

        language = "zh" if is_chinese_source else "en"
        opts = {
            "work_dir": str(self.d),
            "language": language,
            "translate_enabled": True,
            "translation_only": translation_only,
            "_detected_lang": detected_lang,
            "_ffmpeg": "ffmpeg.exe" if mkv_ok is not None else None,
            "_is_stopped": lambda: False,
        }
        posts = []

        mocks = {
            "TranslationClient": MagicMock(),
            "embed_subtitles_to_video": MagicMock(return_value=(None, False)),
            # 本地翻译路径会探测/拉起 llama-server，测试中一律视为已就绪
            "ensure_running": MagicMock(return_value=(True, "ok", False)),
        }

        def mock_client_side_effect(*args, **kwargs):
            client = MagicMock()
            if is_chinese_source:
                # 返回原文（不翻译）
                client.translate_blocks.return_value = [b.text for b in blocks]
            else:
                # 返回翻译文本
                client.translate_blocks.return_value = [f"{b.text}（中文）" for b in blocks]
            client.get_cache_size.return_value = 5
            mocks["client"] = client
            return client

        mocks["TranslationClient"].side_effect = mock_client_side_effect

        if mkv_ok:
            mocks["embed_subtitles_to_video"] = MagicMock(
                return_value=(self.d / "test.mkv", True))

        with patch.multiple(
            "subtitle_app.translator",
            TranslationClient=mocks["TranslationClient"],
            embed_subtitles_to_video=mocks["embed_subtitles_to_video"],
            ensure_running=mocks["ensure_running"],
        ):
            from subtitle_app.translator import translate_only
            translate_only(
                srt_path, self.d, item, 0, 1, opts, posts.append,
            )

        return posts, mocks

    def test_chinese_conversion_skips_model_and_writes_once(self):
        """无需翻译时，不启动模型，简体转换完成后只写一次临时字幕。"""
        from subtitle_app.srt_utils import write_srt
        with patch("subtitle_app.translator.write_srt", wraps=write_srt) as writer, \
                patch("subtitle_app.translator.to_simplified", return_value="汉字"):
            posts, mocks = self._run(srt_texts=["漢字"], detected_lang="zh",
                                     is_chinese_source=True)
        mocks["ensure_running"].assert_not_called()
        mocks["client"].translate_blocks.assert_not_called()
        self.assertEqual(writer.call_count, 1)
        output = next(p["path"] for p in posts if p["type"] == "output_path")
        self.assertIn("汉字", Path(output).read_text(encoding="utf-8"))

    def test_translate_output_modes(self):
        """双语/纯译文写入对应内容；预览译文列不重复原文。"""
        from subtitle_app.srt_utils import parse_srt
        for translation_only in (False, True):
            with self.subTest(translation_only=translation_only):
                posts, mocks = self._run(srt_texts=["Hello", "World"],
                                         translation_only=translation_only)
                mocks["ensure_running"].assert_called_once()
                preview = next(p["message"] for p in posts if p["type"] == "preview")
                self.assertEqual(preview.splitlines()[:4], [
                    "1", "00:00:00,000 --> 00:00:02,000", "Hello", "Hello（中文）"])
                output = next(p["path"] for p in posts if p["type"] == "output_path")
                expected = [f"{src}（中文）" if translation_only else f"{src}\n{src}（中文）"
                            for src in ("Hello", "World")]
                self.assertEqual([b.text for b in parse_srt(Path(output))], expected)

    def test_batch_size_mode_defaults(self):
        """批大小默认读 cfg.translation.batch_size；显式传入时覆盖"""
        captured = {}

        def capture(*args, **kwargs):
            captured["kwargs"] = kwargs
            client = MagicMock()
            client.translate_blocks.return_value = ["（中文）"]
            client.get_cache_size.return_value = 0
            return client

        def run(bs_marker="absent", names=None):
            srt = self.d / "bs.srt"
            blocks = [SubtitleBlock(index=1, start=0.0, end=2.0, text="Hello")]
            from subtitle_app.srt_utils import write_srt
            write_srt(srt, blocks, ["Hello"])
            item = self.d / "bs.mp4"
            item.write_text("dummy")
            opts = {
                "work_dir": str(self.d),
                "language": "en",
                "translate_enabled": True,
                "translation_only": False,
                "_detected_lang": "en",
                "_ffmpeg": None,
                "_is_stopped": lambda: False,
            }
            if bs_marker != "absent":
                opts["translation_batch_size"] = bs_marker
            if names is not None:
                opts["name_glossaries"] = names
            from subtitle_app.translator import translate_only
            with patch.multiple(
                "subtitle_app.translator",
                TranslationClient=MagicMock(side_effect=capture),
                ensure_running=MagicMock(return_value=(True, "ok", False)),
            ):
                translate_only(srt, self.d, item, 0, 1, opts, lambda *a: None)

        from subtitle_app.config import cfg
        # 默认读 config 的 batch_size
        captured.clear()
        run()
        self.assertEqual(captured["kwargs"]["batch_size"], cfg.translation.batch_size or 20)
        # 显式传入覆盖默认
        captured.clear()
        run(bs_marker=50)
        self.assertEqual(captured["kwargs"]["batch_size"], 50)
        captured.clear()
        run(names={"zh": {"ぬね": "甲名"}, "en": {"ぬね": "Nune"}})
        self.assertEqual(captured["kwargs"]["name_translations"], {"ぬね": "甲名"})

    def test_mkv_embed_success_and_fallback(self):
        """MKV 内嵌成功路径；失败 → 回退外挂 SRT"""
        posts, mocks = self._run(srt_texts=["Hello", "World"], mkv_ok=True)
        mocks["embed_subtitles_to_video"].assert_called_once()
        # 应有 output_path 消息
        outputs = [p for p in posts if p["type"] == "output_path"]
        self.assertGreater(len(outputs), 0)
        # MKV 内嵌失败 → 回退外挂 SRT
        posts, mocks = self._run(srt_texts=["Hello", "World"], mkv_ok=False)
        mocks["embed_subtitles_to_video"].assert_called_once()
        outputs = [p for p in posts if p["type"] == "output_path"]
        self.assertGreater(len(outputs), 0)
        # 输出路径应该是 .srt 文件
        self.assertTrue(outputs[-1]["path"].endswith(".srt"))

    def test_state_path_passed_and_cleaned_for_first_run_and_resume(self):
        """首次翻译也必须传入断点路径；续翻提示与成功清理均保留。"""
        state_path = self.d / "test.translate_state.json"
        for resume in (False, True):
            with self.subTest(resume=resume):
                self.assertFalse(state_path.exists())
                if resume:
                    state_path.write_text(json.dumps({"done": {"0": "Hello（中文）"}},
                                                     ensure_ascii=False), encoding="utf-8")
                posts, mocks = self._run(srt_texts=["Hello", "World"])
                call = mocks["client"].translate_blocks
                call.assert_called_once()
                args, kwargs = call.call_args
                self.assertEqual(args[3] if len(args) > 3 else kwargs.get("state_path"),
                                 state_path)
                logs = [p["message"] for p in posts if p["type"] == "log"]
                self.assertEqual(any("断点续翻" in message for message in logs), resume)
                self.assertFalse(state_path.exists())


class TestPassthroughOutputAndProgress(unittest.TestCase):
    """不翻译时的字幕输出与完成记录。"""

    def test_passthrough_output_and_progress(self):
        from subtitle_app.srt_utils import IGNORE_FILE, parse_srt
        from subtitle_app.translator import translate_only
        for suffix in (".mp3", ".mp4"):
            with self.subTest(suffix=suffix), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                srt_path = _make_srt(root / "test.srt")
                item = root / ("test" + suffix)
                item.write_text("dummy")
                posts = []
                translate_only(srt_path, root, item, 0, 1, {
                    "work_dir": str(root), "language": "en", "translate_enabled": False,
                }, posts.append)
                previews = [p for p in posts if p["type"] == "preview"]
                self.assertEqual(len(previews), 1)
                self.assertIn("Hello world", previews[0]["message"])
                output = Path(next(p["path"] for p in posts if p["type"] == "output_path"))
                self.assertEqual(output.parent, root)
                self.assertEqual([b.text for b in parse_srt(output)],
                                 ["Hello world", "This is a test"])
                data = json.loads((root / IGNORE_FILE).read_text(encoding="utf-8"))
                self.assertIn(str(item.resolve()), data["done"])

    def test_record_progress_concurrent_no_lost_update(self):
        """回归：并行线程同时记录进度 → 不抛异常、全部条目保留（无丢失更新）"""
        import threading

        from subtitle_app.translator import _record_progress

        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            items = [d / f"f{i}.mp4" for i in range(8)]
            for p in items:
                p.write_bytes(b"")
            barrier = threading.Barrier(len(items))
            errors = []

            def worker(i):
                barrier.wait()
                try:
                    _record_progress(str(d), items[i])
                except Exception as e:  # noqa: BLE001
                    errors.append(e)

            ts = [threading.Thread(target=worker, args=(i,)) for i in range(len(items))]
            for t in ts:
                t.start()
            for t in ts:
                t.join()

            self.assertEqual(errors, [], f"并发记录进度不应抛异常: {errors}")
            from subtitle_app.srt_utils import IGNORE_FILE, load_json
            data = load_json(d / IGNORE_FILE, {})
            self.assertEqual(len(data.get("done", [])), len(items),
                             "并发写进度文件丢失了条目")


class TestTranslateOnlyMissingSource(unittest.TestCase):
    """源字幕不存在（如被同批视频任务内嵌后清理）→ 静默跳过，不报翻译失败"""

    def test_missing_source_srt_skips(self):
        from subtitle_app.translator import translate_only
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            missing = d / "gone.srt"  # 不创建该文件：模拟已被同批视频任务清理
            item = d / "gone.mp4"
            item.write_text("dummy")
            bumped = []
            opts = {"work_dir": str(d), "language": "en", "translate_enabled": True,
                    "_is_stopped": lambda: False,
                    "_bump_translated": lambda cache: bumped.append(cache)}
            posts = []
            translate_only(missing, d, item, 0, 1, opts, posts.append)
            # 不发 error，有跳过 WARNING 日志
            types = [p["type"] for p in posts]
            self.assertNotIn("error", types)
            warns = [p for p in posts
                     if p["type"] == "log" and p.get("level") == "WARNING"]
            self.assertTrue(warns, "应记录跳过原因")
            # 视为已完成：bump 翻译计数 + 记录进度，避免下次反复尝试
            self.assertEqual(bumped, [0])
            progress_file = d / "cache" / ".subtitle_ignore.json"
            self.assertTrue(progress_file.exists(), "跳过也应记录进度")


class TestPreviewTranslation(unittest.TestCase):
    """_preview_translation：预览译文列不应重复显示原文"""

    def test_strips_original_only_when_leading(self):
        from subtitle_app.translator import _preview_translation
        cases = [
            # 双语合并 "原文\n译文" → 只取译文
            ("Hello", "Hello\n你好", "你好"),
            # 纯译文原样
            ("Hello", "你好", "你好"),
            # 译文本身多行，去掉原文前缀后保留全部译文行
            ("Hello", "Hello\n第一行\n第二行", "第一行\n第二行"),
            # 译文不以原文开头（整句重译）不拆分
            ("Hello", "你好世界\n再见", "你好世界\n再见"),
        ]
        for src, t, expected in cases:
            with self.subTest(t=t):
                self.assertEqual(_preview_translation(src, t), expected)


class TestPruneBackups(unittest.TestCase):
    """_prune_backups：备份保留上限，超出删除最旧"""

    def test_prune_limits_and_disabled(self):
        from subtitle_app.translator import _prune_backups
        import os
        import time
        # 超出上限 → 保留最新 max 份，删最旧
        with tempfile.TemporaryDirectory() as d:
            for i in range(5):
                p = Path(d) / f"test_{i}.srt"
                p.write_text("x", encoding="utf-8")
                t = time.time() - (5 - i) * 10   # test_4 最新，test_0 最旧
                os.utime(p, (t, t))
            _prune_backups(Path(d), 3)
            self.assertEqual(sorted(p.name for p in Path(d).glob("*.srt")),
                             ["test_2.srt", "test_3.srt", "test_4.srt"])
        # max=0 → 不清理（禁用）
        with tempfile.TemporaryDirectory() as d:
            for i in range(5):
                (Path(d) / f"t{i}.srt").write_text("x", encoding="utf-8")
            _prune_backups(Path(d), 0)
            self.assertEqual(len(list(Path(d).glob("*.srt"))), 5)


class TestBatchSizePersistenceField(unittest.TestCase):
    """永久保存批大小：qt_app._batch_size_save_field"""

    def test_field_selection(self):
        from subtitle_app.qt_app import _batch_size_save_field
        # 自定义 → batch_size
        self.assertEqual(
            _batch_size_save_field({"translation_batch_size": 50}),
            ("batch_size", 50))
        # 等于默认值（None=跟随 config）→ 不覆盖 config
        self.assertIsNone(_batch_size_save_field({"translation_batch_size": None}))


if __name__ == "__main__":
    unittest.main()
