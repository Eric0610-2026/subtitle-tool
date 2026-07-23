#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""translator.py 单元测试"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch, call

from subtitle_app.srt_utils import SubtitleBlock


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


class TestTranslateOnlyEarlyReturn(unittest.TestCase):
    """translate_only 的早期返回路径"""

    def test_early_return_if_stopped(self):
        """is_stopped() 返回 True 时直接返回"""
        with tempfile.TemporaryDirectory() as d:
            srt_path = Path(d) / "test.srt"
            _make_srt(srt_path)
            item = Path(d) / "test.mp4"
            item.write_text("dummy")

            opts = {"work_dir": d, "language": "en", "translate_enabled": False,
                    "_is_stopped": MagicMock(return_value=True)}
            posts = []

            from subtitle_app.translator import translate_only
            translate_only(srt_path, Path(d), item, 0, 1, opts, posts.append)

            # 应该没有 post 调用（第一个 status 已在 translate_stage 中发出）
            self.assertEqual(len(posts), 0)

    def test_early_return_if_stopped_after_parse(self):
        """第二个 is_stopped 检查点"""
        with tempfile.TemporaryDirectory() as d:
            srt_path = Path(d) / "test.srt"
            _make_srt(srt_path)
            item = Path(d) / "test.mp4"
            item.write_text("dummy")

            opts = {"work_dir": d, "language": "en", "translate_enabled": False}
            posts = []
            # 第一次返回 False（通过第一个检查），第二次返回 True（在 parse 后返回）
            is_stopped = MagicMock(side_effect=[False, True])
            opts["_is_stopped"] = is_stopped

            from subtitle_app.translator import translate_only
            translate_only(srt_path, Path(d), item, 0, 1, opts, posts.append)

            # 应该有 log 消息（解析字幕），然后停止
            self.assertGreater(len(posts), 0)
            self.assertEqual(posts[0]["type"], "log")


class TestTranslateOnlyPassthrough(unittest.TestCase):
    """translate_only 不翻译（翻译禁用）"""

    def test_no_translate_passthrough(self):
        with tempfile.TemporaryDirectory() as d:
            srt_path = Path(d) / "test.srt"
            _make_srt(srt_path)
            item = Path(d) / "test.mp4"
            item.write_text("dummy")

            opts = {"work_dir": d, "language": "en", "translate_enabled": False}
            posts = []

            from subtitle_app.translator import translate_only
            translate_only(srt_path, Path(d), item, 0, 1, opts, posts.append)

            # 应有 preview 消息
            previews = [p for p in posts if p["type"] == "preview"]
            self.assertEqual(len(previews), 1)
            self.assertIn("Hello world", previews[0]["message"])


class TestTranslateOnlyWithTranslation(unittest.TestCase):
    """translate_only 翻译路径"""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.d = Path(self.tempdir.name)

    def tearDown(self):
        self.tempdir.cleanup()

    def _run(self, srt_texts=None, detected_lang="en", translation_only=False,
             is_chinese_source=False, mkv_ok=False):
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
            "api_url": "https://api.example.com",
            "api_key": "sk-test",
            "translation_model": "gpt-4",
            "translation_only": translation_only,
            "_detected_lang": detected_lang,
            "_ffmpeg": "ffmpeg.exe" if mkv_ok else None,
            "_is_stopped": lambda: False,
        }
        posts = []

        mocks = {
            "TranslationClient": MagicMock(),
            "embed_subtitles_to_video": MagicMock(return_value=(None, False)),
        }

        def mock_client_side_effect(*args, **kwargs):
            client = MagicMock()
            if translation_only or is_chinese_source:
                # 返回原文（不翻译）
                client.translate_blocks.return_value = [b.text for b in blocks]
            else:
                # 返回翻译文本
                client.translate_blocks.return_value = [f"{b.text}（中文）" for b in blocks]
            client.get_cost_info.return_value = {"input_tokens": 10, "cost": 0.001}
            client.get_cache_size.return_value = 5
            return client

        mocks["TranslationClient"].side_effect = mock_client_side_effect

        if mkv_ok:
            mocks["embed_subtitles_to_video"] = MagicMock(
                return_value=(self.d / "test.mkv", True))

        with patch.multiple(
            "subtitle_app.translator",
            TranslationClient=mocks["TranslationClient"],
            embed_subtitles_to_video=mocks["embed_subtitles_to_video"],
        ):
            from subtitle_app.translator import translate_only
            translate_only(
                srt_path, self.d, item, 0, 1, opts, posts.append,
            )

        return posts, mocks

    def test_translate_bilingual(self):
        """双语模式：文本 + 中文翻译"""
        posts, mocks = self._run(srt_texts=["Hello", "World"])
        previews = [p for p in posts if p["type"] == "preview"]
        self.assertGreater(len(previews), 0)
        lines = previews[0]["message"].split("\n")
        # 双语模式下应有翻译内容或原文
        self.assertIn("Hello", lines[0])

    def test_translate_only_mode(self):
        """仅翻译模式：只输出中文"""
        posts, mocks = self._run(srt_texts=["Hello", "World"], translation_only=True)
        previews = [p for p in posts if p["type"] == "preview"]
        self.assertGreater(len(previews), 0)

    def test_chinese_source_simplified(self):
        """中文源 → 繁简转换"""
        blocks = [SubtitleBlock(index=1, start=1.0, end=3.0, text="Hello")]
        srt_path = self.d / "test.srt"
        from subtitle_app.srt_utils import write_srt
        write_srt(srt_path, blocks, [b.text for b in blocks])

        item = self.d / "test.mp4"
        item.write_text("dummy")

        opts = {
            "work_dir": str(self.d),
            "language": "zh",
            "translate_enabled": True,
            "api_url": "https://api.example.com",
            "api_key": "sk-test",
            "translation_model": "gpt-4",
            "translation_only": False,
            "_detected_lang": "zh",
            "_ffmpeg": None,
        }
        posts = []

        # 模拟 translate_blocks 返回：中文源时 has_chinese 会跳过翻译
        # 没有 need_translate_idx，直接走 zh_texts = [b.text for b in blocks]
        with patch("subtitle_app.translator.TranslationClient") as MockClient:
            client = MagicMock()
            client.translate_blocks.return_value = ["Hello"]
            client.get_cost_info.return_value = {}
            client.get_cache_size.return_value = 0
            MockClient.return_value = client

            from subtitle_app.translator import translate_only
            translate_only(srt_path, self.d, item, 0, 1, opts, posts.append)

        # 应输出繁简转换后的文本
        previews = [p for p in posts if p["type"] == "preview"]
        self.assertGreater(len(previews), 0)

    def test_mkv_embed_success(self):
        """MKV 内嵌成功路径"""
        posts, mocks = self._run(srt_texts=["Hello", "World"], mkv_ok=True)
        mocks["embed_subtitles_to_video"].assert_called_once()
        # 应有 output_path 消息
        outputs = [p for p in posts if p["type"] == "output_path"]
        self.assertGreater(len(outputs), 0)

    def test_mkv_embed_failure_fallback(self):
        """MKV 内嵌失败 → 回退外挂 SRT"""
        posts, mocks = self._run(srt_texts=["Hello", "World"], mkv_ok=False)
        outputs = [p for p in posts if p["type"] == "output_path"]
        self.assertGreater(len(outputs), 0)
        # 输出路径应该是 .srt 文件
        self.assertTrue(outputs[-1]["path"].endswith(".srt"))

    def test_state_file_checkpoint(self):
        """断点续翻：state 文件存在"""
        srt_path = self.d / "test.srt"
        blocks = [SubtitleBlock(index=1, start=1.0, end=3.0, text="Hello"),
                  SubtitleBlock(index=2, start=4.0, end=6.0, text="World")]
        from subtitle_app.srt_utils import write_srt
        write_srt(srt_path, blocks, [b.text for b in blocks])

        # 创建 state 文件
        state_path = srt_path.with_name(srt_path.stem + ".translate_state.json")
        state_path.write_text(json.dumps({"done": {"0": "Hello（中文）"}}, ensure_ascii=False), encoding="utf-8")

        item = self.d / "test.mp4"
        item.write_text("dummy")

        opts = {
            "work_dir": str(self.d),
            "language": "en",
            "translate_enabled": True,
            "api_url": "https://api.example.com",
            "api_key": "sk-test",
            "translation_model": "gpt-4",
            "translation_only": False,
            "_detected_lang": "en",
            "_ffmpeg": None,
        }
        posts = []

        with patch("subtitle_app.translator.TranslationClient") as MockClient:
            client = MagicMock()
            client.translate_blocks.return_value = ["Hello（中文）", "World（中文）"]
            client.get_cost_info.return_value = {}
            client.get_cache_size.return_value = 0
            MockClient.return_value = client

            from subtitle_app.translator import translate_only
            translate_only(srt_path, self.d, item, 0, 1, opts, posts.append)

        # 日志中包含断点续翻信息
        logs = [p for p in posts if p["type"] == "log"]
        self.assertTrue(any("断点续翻" in m["message"] for m in logs))

        # state 文件应被清理
        self.assertFalse(state_path.exists())


class TestTranslateOnlyNonVideo(unittest.TestCase):
    """非视频文件 → 外挂字幕"""

    def test_non_video_output(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            srt_path = d / "test.srt"
            _make_srt(srt_path)
            item = d / "test.mp3"  # 音频文件
            item.write_text("dummy")

            opts = {
                "work_dir": str(d),
                "language": "en",
                "translate_enabled": False,
            }
            posts = []

            from subtitle_app.translator import translate_only
            translate_only(srt_path, d, item, 0, 1, opts, posts.append)

            outputs = [p for p in posts if p["type"] == "output_path"]
            self.assertGreater(len(outputs), 0)
            # 非视频应输出到 source_srt 同目录
            self.assertIn(str(d), outputs[-1]["path"])


class TestTranslateOnlyProgressFile(unittest.TestCase):
    """进度文件记录"""

    def test_progress_file_written(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            srt_path = d / "test.srt"
            _make_srt(srt_path)
            item = d / "test.mp4"
            item.write_text("dummy")

            from subtitle_app.srt_utils import IGNORE_FILE
            opts = {
                "work_dir": str(d),
                "language": "en",
                "translate_enabled": False,
            }
            posts = []

            from subtitle_app.translator import translate_only
            translate_only(srt_path, d, item, 0, 1, opts, posts.append)

            progress_file = d / IGNORE_FILE
            self.assertTrue(progress_file.exists())
            data = json.loads(progress_file.read_text(encoding="utf-8"))
            self.assertIn("done", data)
            self.assertIn(str(item.resolve()), data["done"])


if __name__ == "__main__":
    unittest.main()