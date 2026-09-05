#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""translation 单元测试（适配当前代码）"""
import unittest
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch
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
    def test_json_variants(self):
        cases = [
            ('{"items": []}', {"items": []}),          # 直接 JSON
            ('```json\n{"a":1}\n```', {"a": 1}),       # 代码块包裹
            ('前缀 {"b":2} 后缀', {"b": 2}),            # 裸花括号
        ]
        for raw, expected in cases:
            with self.subTest(raw=raw):
                self.assertEqual(_extract_json(raw), expected)
        self.assertIsNone(_extract_json("完全不是json xyz"))


class TestComposeSentences(unittest.TestCase):
    def test_compose_variants(self):
        self.assertEqual(_compose_sentences(["你好", "世界"]), "你好世界")
        self.assertEqual(_compose_sentences(["Hello", "world"]), "Hello world")
        self.assertEqual(_compose_sentences(["你好", "world"]), "你好 world")


class TestParseResponse(unittest.TestCase):
    def setUp(self):
        self.client = TranslationClient("url", "key", "m",
                                        Path(tempfile.mktemp()), lambda *a: None)

    def test_response_formats(self):
        # OpenAI 格式
        resp = {"choices": [{"message": {"content": '{"items":[{"id":1,"zh":"你好"}]}'}}]}
        items = self.client._parse_translation_response(resp)
        self.assertEqual(items[0]["zh"], "你好")
        # 字符串列表
        resp = {"items": ["你好", "世界"]}
        items = self.client._parse_translation_response(resp)
        self.assertEqual([i["zh"] for i in items], ["你好", "世界"])
        # dict 列表 text 别名
        resp = {"items": [{"id": 1, "text": "你好"}]}
        items = self.client._parse_translation_response(resp)
        self.assertEqual(items[0]["zh"], "你好")


class TestTranslationClient(unittest.TestCase):
    @staticmethod
    def _client(d, **kw):
        return TranslationClient("url", "key", "m",
                                 Path(d) / "cache.json", lambda *a: None, **kw)

    def test_translate_blocks_and_cache(self):
        # 默认批次大小应跟随 config 的 translation.batch_size
        from subtitle_app.config import cfg
        c = TranslationClient("url", "key", "m", Path(tempfile.mktemp()), lambda *a: None)
        self.assertEqual(c.batch_size, cfg.translation.batch_size)
        # 基本翻译 + 空缓存大小
        with tempfile.TemporaryDirectory() as d:
            c = self._client(d, batch_size=10)
            self.assertEqual(c.get_cache_size(), 0)
            blocks = [SubtitleBlock(index=1, start=0, end=1, text="Hello"),
                      SubtitleBlock(index=2, start=1, end=2, text="World")]
            c._translate_batch = lambda texts, context="", depth=0: [
                {"id": i + 1, "zh": f"译{t}"} for i, t in enumerate(texts)]
            self.assertEqual(c.translate_blocks(blocks, "en", is_bilingual=True),
                             ["译Hello", "译World"])
        # 第二次调用缓存命中，不再调 _translate_batch
        with tempfile.TemporaryDirectory() as d:
            c = self._client(d, batch_size=10)
            blocks = [SubtitleBlock(index=1, start=0, end=1, text="Hello")]
            mock = MagicMock(return_value=[{"id": 1, "zh": "译Hello"}])
            c._translate_batch = mock
            c.translate_blocks(blocks, "en", is_bilingual=True)
            self.assertEqual(mock.call_count, 1)
            c.translate_blocks(blocks, "en", is_bilingual=True)
            self.assertEqual(mock.call_count, 1)

    def test_batch_size_zero_or_negative_clamped(self):
        """batch_size=0/负数会导致分批 range(0, len, 0) / `// 0` 崩溃，应钳位到至少 1"""
        c = TranslationClient("url", "key", "m", Path(tempfile.mktemp()),
                              lambda *a: None, batch_size=0)
        self.assertGreaterEqual(c.batch_size, 1)
        c2 = TranslationClient("url", "key", "m", Path(tempfile.mktemp()),
                               lambda *a: None, batch_size=-3)
        self.assertGreaterEqual(c2.batch_size, 1)

    def test_curl_fallback_tmp_file_unique_per_thread(self):
        """并发 403 fallback 时各线程的临时文件必须唯一，不能共写一个文件"""
        from subtitle_app import translation as tr
        with tempfile.TemporaryDirectory() as d:
            c = self._client(d, batch_size=10)
            seen = {}

            def fake_save_json(path, payload):
                seen[threading.get_ident()] = str(path)

            ok_resp = MagicMock(returncode=0, stdout='{"id": 1}')
            with patch.object(tr.shutil, "which", return_value="curl.exe"), \
                    patch.object(tr, "save_json", side_effect=fake_save_json), \
                    patch.object(tr, "subprocess_run_safe", return_value=ok_resp):
                threads = [threading.Thread(
                    target=lambda: c._curl_fallback({"text": "x"}, {"A": "b"}))
                    for _ in range(4)]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join()
            self.assertEqual(len(seen), 4)  # 4 个线程各自生成了不同文件名


class TestParagraphContext(unittest.TestCase):
    """段落上下文（Bug #3）：同段批次串行带上文，跨段批次互不阻塞"""

    @staticmethod
    def _client(d, **kw):
        return TranslationClient("url", "key", "m",
                                 Path(d) / "cache.json", lambda *a: None, **kw)

    @staticmethod
    def _fake_translate(calls):
        def fake(texts, context="", depth=0):
            calls.append((list(texts), context))
            return [{"id": i + 1, "zh": f"译{t}"} for i, t in enumerate(texts)]
        return fake

    def test_same_para_batch_gets_previous_translation_as_context(self):
        """同段两批（batch_size=1）：第二批的 context 应包含第一批的译文"""
        with tempfile.TemporaryDirectory() as d:
            c = self._client(d, batch_size=1)
            # 两字幕时间连续（gap=0）→ 同一段落
            blocks = [SubtitleBlock(index=1, start=0, end=1, text="Hello one"),
                      SubtitleBlock(index=2, start=1, end=2, text="Hello two")]
            calls = []
            c._translate_batch = self._fake_translate(calls)
            c.translate_blocks(blocks, "en", is_bilingual=True)
            self.assertEqual(len(calls), 2)
            # 第一批无上文
            self.assertEqual(calls[0][1], "")
            # 第二批带上第一批译文（段内上下文连续）
            self.assertIn("（上文）译Hello one", calls[1][1])

    def test_cross_para_batches_not_blocked(self):
        """不同段落的批次不互相等待：一批阻塞时另一批仍开始执行"""
        with tempfile.TemporaryDirectory() as d:
            c = self._client(d, batch_size=1)
            # 时间间隔超过 PARAGRAPH_GAP → 不同段落
            blocks = [SubtitleBlock(index=1, start=0, end=1, text="Hello one"),
                      SubtitleBlock(index=2, start=10, end=11, text="Hello two")]
            entered = threading.Event()
            release = threading.Event()
            calls = []

            def fake(texts, context="", depth=0):
                calls.append((list(texts), context))
                if len(calls) == 1:
                    entered.set()      # 第一个批次开始且阻塞
                    release.wait(5)
                return [{"id": i + 1, "zh": f"译{t}"} for i, t in enumerate(texts)]

            c._translate_batch = fake
            out = {}
            t = threading.Thread(target=lambda: out.setdefault(
                "r", c.translate_blocks(blocks, "en", is_bilingual=True)))
            t.start()
            try:
                self.assertTrue(entered.wait(3), "第一批应已开始")
                deadline = time.time() + 3
                while len(calls) < 2 and time.time() < deadline:
                    time.sleep(0.05)
                # 第一批仍在阻塞，第二批（不同段）已开始 → 未互相等待
                self.assertGreaterEqual(len(calls), 2)
                # 跨段批次没有可用的上文（两批互为独立段落）
                self.assertEqual(calls[1][1], "")
            finally:
                release.set()
                t.join(5)
            self.assertFalse(t.is_alive(), "translate_blocks 应正常结束")


class TestRecursionProtection(unittest.TestCase):
    """递归深度保护测试"""

    def test_recursion_and_fallback_paths(self):
        """递归深度保护 + 各类缓存/断点/回填路径共用一套覆盖"""
        from subtitle_app.translation import MAX_RECURSION_DEPTH
        client = TranslationClient("url", "key", "m",
                                   Path(tempfile.mktemp()), lambda *a: None, batch_size=5)
        # 超过递归深度 → 返回原文
        def always_fail(texts_, context="", depth=0):
            raise RuntimeError("mock fail")
        client._call_api = always_fail
        result = client._translate_batch(["a", "b", "c"], depth=MAX_RECURSION_DEPTH)
        self.assertEqual([item["zh"] for item in result], ["a", "b", "c"])
        # 单句 API 失败 → 纯文本兜底
        def raise_err(payload, headers):
            raise RuntimeError("fail")
        client._call_api = raise_err
        result = client._translate_batch(["hello"])
        self.assertEqual(result[0]["zh"], "hello")
        # 中文源/缓存全命中时返回译文；缓存第二次命中不调 API
        with tempfile.TemporaryDirectory() as d:
            c = TranslationClient("url", "key", "m",
                                  Path(d) / "cache.json", lambda *a: None, batch_size=10)
            blocks = [SubtitleBlock(index=1, start=0, end=1, text="你好世界")]
            mock = MagicMock(return_value=[{"id": 1, "zh": "你好世界"}])
            c._translate_batch = mock
            self.assertEqual(c.translate_blocks(blocks, "zh", is_bilingual=True),
                             ["你好世界"])
        with tempfile.TemporaryDirectory() as d:
            c = TranslationClient("url", "key", "m",
                                  Path(d) / "cache.json", lambda *a: None, batch_size=10)
            blocks = [SubtitleBlock(index=1, start=0, end=1, text="Hello world")]
            mock = MagicMock(return_value=[{"id": 1, "zh": "你好世界"}])
            c._translate_batch = mock
            res = c.translate_blocks(blocks, "en", is_bilingual=True)
            mock.assert_called_once()
            self.assertEqual(res, ["你好世界"])
            mock.reset_mock()
            res2 = c.translate_blocks(blocks, "en", is_bilingual=True)
            mock.assert_not_called()
            self.assertEqual(res2, ["你好世界"])
        # 缓存/断点 state 里的空串不应阻止重新翻译
        from subtitle_app.srt_utils import sentence_cache_key
        with tempfile.TemporaryDirectory() as d:
            c = TranslationClient("url", "key", "m",
                                  Path(d) / "cache.json", lambda *a: None, batch_size=10)
            blocks = [SubtitleBlock(index=1, start=0, end=1, text="Hello")]
            key = sentence_cache_key("Hello", c.model, True)
            c.cache[key] = ""
            c._translate_batch = lambda texts, context="", depth=0: [
                {"id": 1, "zh": "你好"}]
            self.assertEqual(c.translate_blocks(blocks, "en", is_bilingual=True),
                             ["你好"])
        with tempfile.TemporaryDirectory() as d:
            c = TranslationClient("url", "key", "m",
                                  Path(d) / "cache.json", lambda *a: None, batch_size=10)
            blocks = [SubtitleBlock(index=1, start=0, end=1, text="Hello")]
            state = Path(d) / "t.translate_state.json"
            state.write_text(
                '{"done":{"0":""},"originals":{"0":"Hello"}}',
                encoding="utf-8",
            )
            c._translate_batch = lambda texts, context="", depth=0: [
                {"id": 1, "zh": "你好"}]
            res = c.translate_blocks(blocks, "en", is_bilingual=True, state_path=state)
            self.assertEqual(res, ["你好"])
        # API 漏 id 时按顺序回填，不整块丢弃
        with tempfile.TemporaryDirectory() as d:
            c = TranslationClient("url", "key", "m",
                                  Path(d) / "cache.json", lambda *a: None, batch_size=10)
            blocks = [
                SubtitleBlock(index=1, start=0, end=1, text="Hello"),
                SubtitleBlock(index=2, start=1, end=2, text="World"),
            ]
            # 无 id，仅按顺序返回
            c._translate_batch = lambda texts, context="", depth=0: [
                {"zh": f"译{t}"} for t in texts]
            res = c.translate_blocks(blocks, "en", is_bilingual=True)
            self.assertEqual(res, ["译Hello", "译World"])

    def test_reassemble_partial_sentences(self):
        """多句块中部分成功时，应拼合「译文+未译原文」而非整块回退"""
        from subtitle_app.translation import _reassemble_blocks
        blocks = [SubtitleBlock(1, 0, 2, "Hello. World.")]
        flat = [(0, 0, "Hello."), (1, 0, "World.")]
        sent_trans = {0: "你好。", 1: ""}  # 第二句未译
        out = _reassemble_blocks(blocks, flat, sent_trans)
        self.assertEqual(len(out), 1)
        self.assertIn("你好。", out[0])
        self.assertIn("World.", out[0])


class TestSentenceSplitting(unittest.TestCase):
    """句子拆分扩展测试"""

    def test_abbreviation_chinese_and_cache_key(self):
        from subtitle_app.srt_utils import split_sentences, sentence_cache_key
        # Dr. 不应触发句子分割
        self.assertEqual(len(split_sentences("Dr. Smith is here.")), 1)
        # 中文标点拆分
        sents = split_sentences("你好。世界！今天天气怎么样？")
        self.assertGreaterEqual(len(sents), 3)
        # 缓存 key：确定性 + 大小写归一化 + 模型区分
        k1 = sentence_cache_key("Hello", "m", True)
        self.assertEqual(k1, sentence_cache_key("Hello", "m", True))
        self.assertEqual(k1, sentence_cache_key("hello", "m", True))
        self.assertNotEqual(k1, sentence_cache_key("Hello", "m2", True))


class TestChineseDetection(unittest.TestCase):
    """中文检测扩展测试"""

    def test_japanese_and_bilingual_detection(self):
        from subtitle_app.srt_utils import has_chinese
        # 纯假名 → 非中文
        self.assertFalse(has_chinese("こんにちは", "zh"))
        self.assertFalse(has_chinese("カタカナです", "zh"))
        # 日文汉字 + 假名 → 非中文
        self.assertFalse(has_chinese("返事をください"))
        # 含中文字符 → 是中文
        self.assertTrue(has_chinese("Hello\n你好世界"))


class TestSharedCache(unittest.TestCase):
    """进程级共享缓存：多个 TranslationClient 共享同一内存 dict，
    写回磁盘时不再互相覆盖（并发流水线回归测试）"""

    def test_shared_cache_share_persist_switch(self):
        # 同一路径共享同一 dict；后写回不丢先写回的条目
        with tempfile.TemporaryDirectory() as d:
            cache_path = Path(d) / "cache.json"
            c1 = TranslationClient("url", "key", "m", cache_path, lambda *a: None)
            c2 = TranslationClient("url", "key", "m", cache_path, lambda *a: None)
            c1.cache["k1"] = "v1"
            self.assertEqual(c2.cache.get("k1"), "v1")
            c2.cache["k2"] = "v2"
            c2._save_cache()  # 后写回的 client 不能丢先写回的条目
            from subtitle_app.srt_utils import load_json
            disk = load_json(cache_path, {})
            self.assertEqual(disk.get("k1"), "v1")
            self.assertEqual(disk.get("k2"), "v2")
        # 换路径 → 切换独立缓存
        with tempfile.TemporaryDirectory() as d:
            path_a = Path(d) / "a.json"
            path_b = Path(d) / "b.json"
            ca = TranslationClient("url", "key", "m", path_a, lambda *a: None)
            ca.cache["only_a"] = "1"
            cb = TranslationClient("url", "key", "m", path_b, lambda *a: None)
            self.assertNotIn("only_a", cb.cache)
            cb.cache["only_b"] = "1"
            self.assertNotIn("only_b", ca.cache)


class TestApplyBatchTranslations(unittest.TestCase):
    """_apply_batch_translations 的 id 对齐与缓存保护"""

    @staticmethod
    def _run(batch, translations, t2g=None):
        from subtitle_app.translation import _apply_batch_translations
        sent_trans, cache = {}, {}
        lock = threading.Lock()
        t2g = t2g or {t: [i] for i, t in enumerate(batch)}
        applied = _apply_batch_translations(batch, translations, t2g, sent_trans,
                                            cache, lock, "m", True)
        return sent_trans, cache, applied

    def test_zh_equal_orig_and_wrong_count(self):
        # 模型把原文当译文返回（拒译）：不固化进缓存，也不当作有效译文
        sent_trans, cache, applied = self._run(
            ["你好", "世界"], [{"id": 1, "zh": "你好"}])
        self.assertEqual(cache, {})
        self.assertNotIn(0, sent_trans)
        self.assertEqual(applied[0], ("你好", ""))
        # 返回条数不足时不做硬顺序回退：第 j 条译文不会错配给第 i 条
        sent_trans, cache, applied = self._run(
            ["A", "B", "C"], [{"id": 2, "zh": "译B"}])
        self.assertEqual(sent_trans.get(1), "译B")
        self.assertNotIn(0, sent_trans)
        self.assertNotIn(2, sent_trans)


class TestTranslateSplit(unittest.TestCase):
    """递归拆批后第二个子批的 id 需重新编号，避免回填错位"""

    def test_second_half_ids_renumbered(self):
        c = TranslationClient("url", "key", "m", Path(tempfile.mktemp()),
                              lambda *a: None, batch_size=10)
        c._translate_batch = lambda texts, context="", depth=0: [
            {"id": i + 1, "zh": f"译{t}"} for i, t in enumerate(texts)]
        out = c._translate_split(["a", "b", "c", "d"], "", 0)
        ids = [item["id"] for item in out]
        self.assertEqual(ids, [1, 2, 3, 4])
        self.assertEqual([item["zh"] for item in out], ["译a", "译b", "译c", "译d"])

    def test_offset_uses_mid_when_left_short(self):
        """回归：左半批 API 少返回条目时，偏移仍按 mid 计算

        旧逻辑 offset = len(left)：左半批截断丢项时右半批 id 整体前移，
        译文错位并经持久缓存污染后续所有运行。
        """
        c = TranslationClient("url", "key", "m", Path(tempfile.mktemp()),
                              lambda *a: None, batch_size=10)

        def fake_batch(texts, context="", depth=0):
            if texts == ["a", "b"]:
                # 模拟 API 截断：只返回 1 条（丢 "b"）
                return [{"id": 1, "zh": "译a"}]
            return [{"id": i + 1, "zh": f"译{t}"} for i, t in enumerate(texts)]

        c._translate_batch = fake_batch
        out = c._translate_split(["a", "b", "c", "d"], "", 0)
        by_id = {it["id"]: it["zh"] for it in out}
        # 右半批 id 必须从 mid+1=3 开始，不受左半批丢项影响
        self.assertEqual(by_id.get(3), "译c")
        self.assertEqual(by_id.get(4), "译d")
        self.assertNotIn(2, by_id)  # b 丢失就是丢失，不得把 c 的译文顶到 b 的位置


class TestStopAndBreaker(unittest.TestCase):
    """用户停止 + API 熔断：停止快速生效，死 API 不再白烧请求"""

    @staticmethod
    def _client(d, **kw):
        return TranslationClient("url", "key", "m",
                                 Path(d) / "cache.json", lambda *a: None, **kw)

    @staticmethod
    def _blocks(n):
        return [SubtitleBlock(index=i, start=i, end=i + 1, text=f"T{i}")
                for i in range(1, n + 1)]

    def test_stop_check_aborts_pending_batches(self):
        """stop_check 置位后：不再发起新批次，未开始的批次被取消，抛 TranslationStopped"""
        from subtitle_app.translation import TranslationStopped
        with tempfile.TemporaryDirectory() as d:
            c = self._client(d, batch_size=1)
            calls = []
            stop = threading.Event()

            def fake(texts, context="", depth=0):
                calls.append(list(texts))
                if len(calls) >= 2:
                    stop.set()
                return [{"id": i + 1, "zh": f"译{t}"} for i, t in enumerate(texts)]

            c._translate_batch = fake
            with self.assertRaises(TranslationStopped):
                c.translate_blocks(self._blocks(5), "en", True,
                                   translation_concurrency=1, stop_check=stop.is_set)
            self.assertLess(len(calls), 5, "停止后不应继续翻译剩余批次")

    def test_empty_batches_trip_breaker(self):
        """连续多批 0 有效译文（全部拒译）→ 空批熔断中止，不再逐句补翻"""
        with tempfile.TemporaryDirectory() as d:
            c = self._client(d, batch_size=1)
            calls = []
            release = threading.Event()

            def fake(texts, context="", depth=0):
                calls.append(list(texts))
                if len(calls) > 3:
                    # 模拟第 4 批起仍在途：给主线程时间触发熔断并取消排队批次
                    release.wait(5)
                # 拒译：返回原文 → applied 计 0
                return [{"id": i + 1, "zh": t} for i, t in enumerate(texts)]

            c._translate_batch = fake
            try:
                with self.assertRaises(RuntimeError) as ctx:
                    c.translate_blocks(self._blocks(6), "en", True,
                                       translation_concurrency=1)
            finally:
                release.set()
            self.assertIn("无有效译文", str(ctx.exception))
            # 串行执行时第 3 批完成后即熔断；第 4 批若已被 worker 取走属于在途，最多 4 批
            self.assertLessEqual(len(calls), 4)

    def test_network_error_does_not_split(self):
        """网络不可达（ApiUnavailableError）不拆批递归：一次失败即中止"""
        from subtitle_app.translation import ApiUnavailableError
        c = TranslationClient("url", "key", "m", Path(tempfile.mktemp()),
                              lambda *a: None, batch_size=10)
        calls = []

        def fake_api(payload, headers):
            calls.append(1)
            raise ApiUnavailableError("网络错误")

        c._call_api = fake_api
        with self.assertRaises(ApiUnavailableError):
            c._translate_batch(["a", "b", "c", "d"])
        self.assertEqual(len(calls), 1, "网络类错误不应触发拆批重试")

    def test_missing_fix_breaks_after_consecutive_failures(self):
        """补翻兜底熔断：连续 20 句无有效译文 → 放弃剩余补翻（不再拖死文件）"""
        with tempfile.TemporaryDirectory() as d:
            c = self._client(d, batch_size=20)
            logs = []
            c.post_ui = lambda e: logs.append(e) if isinstance(e, dict) and e.get("type") == "log" else None
            # 25 句、批 20 → 主阶段 2 批（不触发空批熔断），全部拒译
            c._translate_batch = lambda texts, context="", depth=0: [
                {"id": i + 1, "zh": t} for i, t in enumerate(texts)]
            res = c.translate_blocks(self._blocks(25), "en", True)
            self.assertEqual(len(res), 25)
            self.assertTrue(any("放弃剩余补翻" in l.get("message", "") for l in logs),
                            "补翻连续失败应触发熔断日志")


class TestSharedCacheManagement(unittest.TestCase):
    """缓存管理 API：就地清空/删除，已创建的 client 同步可见"""

    def test_clear_and_remove_shared_cache(self):
        from subtitle_app.translation import (
            clear_shared_cache, remove_shared_cache_entries, shared_cache_snapshot,
        )
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "cache.json"
            c = TranslationClient("url", "key", "m", path, lambda *a: None)
            c.cache["k1"] = "v1"
            clear_shared_cache()
            self.assertEqual(c.get_cache_size(), 0, "已创建的 client 应看到清空后的缓存")
            c.cache["k2"] = "v2"
            removed = remove_shared_cache_entries(["k2", "不存在的key"])
            self.assertEqual(removed, 1)
            self.assertEqual(c.get_cache_size(), 0)
            c.cache["k3"] = "v3"
            self.assertEqual(shared_cache_snapshot(), {"k3": "v3"})


if __name__ == "__main__":
    unittest.main()
