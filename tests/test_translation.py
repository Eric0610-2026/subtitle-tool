#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""translation 单元测试（适配当前代码）"""
import unittest
import tempfile
from pathlib import Path
from unittest.mock import MagicMock
from subtitle_app.srt_utils import SubtitleBlock

from subtitle_app.translation import (
    TranslationClient, ApiForbiddenError, _extract_json, _compose_sentences,
)


class TestApiForbiddenError(unittest.TestCase):
    def test_is_runtime_error(self):
        self.assertTrue(issubclass(ApiForbiddenError, RuntimeError))
        with self.assertRaises(ApiForbiddenError):
            raise ApiForbiddenError("403")


class TestExtractJson(unittest.TestCase):
    def test_direct(self):
        self.assertEqual(_extract_json('{"items": []}'), {"items": []})

    def test_code_block(self):
        self.assertEqual(_extract_json('```json\n{"a":1}\n```'), {"a": 1})

    def test_bare_braces(self):
        self.assertEqual(_extract_json('前缀 {"b":2} 后缀'), {"b": 2})

    def test_invalid(self):
        self.assertIsNone(_extract_json("完全不是json xyz"))


class TestComposeSentences(unittest.TestCase):
    def test_cjk_no_space(self):
        self.assertEqual(_compose_sentences(["你好", "世界"]), "你好世界")

    def test_english_space(self):
        self.assertEqual(_compose_sentences(["Hello", "world"]), "Hello world")

    def test_mixed(self):
        self.assertEqual(_compose_sentences(["你好", "world"]), "你好 world")


class TestParseResponse(unittest.TestCase):
    def setUp(self):
        self.client = TranslationClient("url", "key", "m",
                                        Path(tempfile.mktemp()), lambda *a: None)

    def test_openai_format(self):
        resp = {"choices": [{"message": {"content": '{"items":[{"id":1,"zh":"你好"}]}'}}]}
        items = self.client._parse_translation_response(resp)
        self.assertEqual(items[0]["zh"], "你好")

    def test_string_list(self):
        resp = {"items": ["你好", "世界"]}
        items = self.client._parse_translation_response(resp)
        self.assertEqual([i["zh"] for i in items], ["你好", "世界"])

    def test_dict_list_alias(self):
        resp = {"items": [{"id": 1, "text": "你好"}]}
        items = self.client._parse_translation_response(resp)
        self.assertEqual(items[0]["zh"], "你好")


class TestTranslationClient(unittest.TestCase):
    @staticmethod
    def _client(d, **kw):
        return TranslationClient("url", "key", "m",
                                 Path(d) / "cache.json", lambda *a: None, **kw)

    def test_default_batch_size(self):
        c = TranslationClient("url", "key", "m", Path(tempfile.mktemp()), lambda *a: None)
        self.assertEqual(c.batch_size, 100)

    def test_translate_blocks_basic(self):
        with tempfile.TemporaryDirectory() as d:
            c = self._client(d, batch_size=10)
            blocks = [SubtitleBlock(index=1, start=0, end=1, text="Hello"),
                      SubtitleBlock(index=2, start=1, end=2, text="World")]
            c._translate_batch = lambda texts, context="", depth=0: [
                {"id": i + 1, "zh": f"译{t}"} for i, t in enumerate(texts)]
            res = c.translate_blocks(blocks, "en", is_bilingual=True)
            self.assertEqual(res, ["译Hello", "译World"])

    def test_cache_hit_skips_api(self):
        with tempfile.TemporaryDirectory() as d:
            c = self._client(d, batch_size=10)
            blocks = [SubtitleBlock(index=1, start=0, end=1, text="Hello")]
            mock = MagicMock(return_value=[{"id": 1, "zh": "译Hello"}])
            c._translate_batch = mock
            c.translate_blocks(blocks, "en", is_bilingual=True)
            self.assertEqual(mock.call_count, 1)
            # 第二次：缓存命中，不再调用 _translate_batch
            c.translate_blocks(blocks, "en", is_bilingual=True)
            self.assertEqual(mock.call_count, 1)

    def test_get_cache_size(self):
        with tempfile.TemporaryDirectory() as d:
            c = self._client(d, batch_size=10)
            self.assertEqual(c.get_cache_size(), 0)


class TestRecursionProtection(unittest.TestCase):
    """递归深度保护测试"""

    def test_depth_limit_returns_original(self):
        from subtitle_app.translation import MAX_RECURSION_DEPTH
        client = TranslationClient("url", "key", "m",
                                   Path(tempfile.mktemp()), lambda *a: None, batch_size=5)
        texts = ["a", "b", "c"]
        # 模拟总是失败的 translate_batch，应在超过递归深度后返回原文
        def always_fail(texts_, context="", depth=0):
            raise RuntimeError("mock fail")
        client._call_api = always_fail
        # depth 从 0 开始，超过 MAX_RECURSION_DEPTH 时直接返回原文
        result = client._translate_batch(texts, depth=MAX_RECURSION_DEPTH)
        self.assertEqual(len(result), 3)
        for item, orig in zip(result, texts):
            self.assertEqual(item["zh"], orig)

    def test_single_text_fallback_plain(self):
        client = TranslationClient("url", "key", "m",
                                   Path(tempfile.mktemp()), lambda *a: None, batch_size=5)
        # 模拟 API 总是失败，单句走纯文本兜底
        def raise_err(payload, headers):
            raise RuntimeError("fail")
        client._call_api = raise_err
        result = client._translate_batch(["hello"])
        self.assertEqual(result[0]["zh"], "hello")

    def test_translate_blocks_chinese_skip(self):
        """中文跳过在 _translate_only 层实现，translate_blocks 本身不做 CJK 判定"""
        with tempfile.TemporaryDirectory() as d:
            c = TranslationClient("url", "key", "m",
                                  Path(d) / "cache.json", lambda *a: None, batch_size=10)
            blocks = [SubtitleBlock(index=1, start=0, end=1, text="你好世界")]
            mock = MagicMock(return_value=[{"id": 1, "zh": "你好世界"}])
            c._translate_batch = mock
            res = c.translate_blocks(blocks, "zh", is_bilingual=True)
            self.assertEqual(res, ["你好世界"])

    def test_translate_blocks_with_cache_all_hit(self):
        """全部缓存命中时不调用 API，且返回正确的译文而非原文"""
        with tempfile.TemporaryDirectory() as d:
            c = TranslationClient("url", "key", "m",
                                  Path(d) / "cache.json", lambda *a: None, batch_size=10)
            blocks = [SubtitleBlock(index=1, start=0, end=1, text="Hello world")]
            mock = MagicMock(return_value=[{"id": 1, "zh": "你好世界"}])
            c._translate_batch = mock
            res = c.translate_blocks(blocks, "en", is_bilingual=True)
            mock.assert_called_once()
            self.assertEqual(res, ["你好世界"])
            # 第二次：缓存命中，不再调用 _translate_batch，且返回译文
            mock.reset_mock()
            res2 = c.translate_blocks(blocks, "en", is_bilingual=True)
            mock.assert_not_called()
            self.assertEqual(res2, ["你好世界"])


class TestSentenceSplitting(unittest.TestCase):
    """句子拆分扩展测试"""

    def test_abbreviation_protection(self):
        from subtitle_app.srt_utils import split_sentences
        sents = split_sentences("Dr. Smith is here.")
        # Dr. 不应触发句子分割
        self.assertEqual(len(sents), 1)

    def test_chinese_sentence_split(self):
        from subtitle_app.srt_utils import split_sentences
        text = "你好。世界！今天天气怎么样？"
        sents = split_sentences(text)
        self.assertGreaterEqual(len(sents), 3)

    def test_cache_key_consistency(self):
        from subtitle_app.srt_utils import sentence_cache_key
        k1 = sentence_cache_key("Hello", "m", True)
        k2 = sentence_cache_key("Hello", "m", True)
        k3 = sentence_cache_key("hello", "m", True)
        self.assertEqual(k1, k2)
        # 大小写归一化
        self.assertEqual(k1, k3)

    def test_cache_key_different_model(self):
        from subtitle_app.srt_utils import sentence_cache_key
        k1 = sentence_cache_key("Hello", "m1", True)
        k2 = sentence_cache_key("Hello", "m2", True)
        self.assertNotEqual(k1, k2)


class TestChineseDetection(unittest.TestCase):
    """中文检测扩展测试"""

    def test_japanese_kana_not_chinese(self):
        from subtitle_app.srt_utils import has_chinese
        self.assertFalse(has_chinese("こんにちは", "zh"))
        self.assertFalse(has_chinese("カタカナです", "zh"))

    def test_japanese_kanji_with_kana(self):
        from subtitle_app.srt_utils import has_chinese
        # 日文汉字 + 假名 = 不应判定为中文
        self.assertFalse(has_chinese("返事をください"))

    def test_bilingual_line(self):
        from subtitle_app.srt_utils import has_chinese
        self.assertTrue(has_chinese("Hello\n你好世界"))


if __name__ == "__main__":
    unittest.main()
