#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""srt_utils 单元测试（适配当前代码）"""
import unittest
import tempfile
from pathlib import Path

from subtitle_app.srt_utils import (
    seconds_to_srt_time, srt_time_to_seconds, fmt_duration,
    SubtitleBlock, parse_srt, parse_srt_text, write_srt, split_sentences,
    sentence_cache_key, to_simplified, has_chinese, safe_stem,
    load_json, save_json, fmt_job_display, find_existing_subtitle,
    match_video_for_subtitle,
    wrap_subtitle_text,
)


class TestTimeUtils(unittest.TestCase):
    def test_time_conversions_and_format(self):
        # 秒 → SRT 时间
        self.assertEqual(seconds_to_srt_time(0), "00:00:00,000")
        self.assertEqual(seconds_to_srt_time(3661.5), "01:01:01,500")
        # 往返
        for sec in (0, 1.0, 59.999, 3661.5, 7200.123):
            self.assertAlmostEqual(
                srt_time_to_seconds(seconds_to_srt_time(sec)), sec, places=2)
        # 时长格式化
        self.assertEqual(fmt_duration(0), "00:00")
        self.assertEqual(fmt_duration(65), "01:05")
        self.assertEqual(fmt_duration(3661), "1:01:01")
        self.assertEqual(fmt_duration(None), "--:--")


class TestSrtRoundtrip(unittest.TestCase):
    @staticmethod
    def _sample(d: Path) -> Path:
        p = d / "a.srt"
        p.write_text(
            "1\n00:00:01,000 --> 00:00:03,000\n你好世界\n\n"
            "2\n00:00:04,000 --> 00:00:06,500\n第二句\n",
            encoding="utf-8")
        return p

    def test_parse_write_timing_and_gbk(self):
        """parse/write 往返 + timing 格式化 + GBK 编码回退"""
        with tempfile.TemporaryDirectory() as d:
            blocks = parse_srt(self._sample(Path(d)))
            self.assertEqual(len(blocks), 2)
            self.assertEqual(blocks[0].text, "你好世界")
            self.assertAlmostEqual(blocks[0].start, 1.0)
            self.assertAlmostEqual(blocks[0].end, 3.0)
            out = Path(d) / "out.srt"
            write_srt(out, blocks, [b.text for b in blocks])
            re = parse_srt(out)
            self.assertEqual(len(re), 2)
            self.assertEqual(re[1].text, "第二句")
        b = SubtitleBlock(index=1, start=1.0, end=3.0, text="x")
        self.assertEqual(b.timing, "00:00:01,000 --> 00:00:03,000")
        # GBK/ANSI 编码的中文字幕也能解析（Windows 常见）
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "gbk.srt"
            p.write_bytes(
                "1\n00:00:01,000 --> 00:00:02,000\n你好世界\n\n"
                "2\n00:00:03,000 --> 00:00:04,000\n第二句\n".encode("gbk"))
            blocks = parse_srt(p)
            self.assertEqual(len(blocks), 2)
            self.assertEqual(blocks[0].text, "你好世界")
            self.assertEqual(blocks[1].text, "第二句")

    def test_parse_edge_cases(self):
        """空文本块在中间保留 + 缺空行不吞块"""
        # 空文本块不在文件末尾时也必须被解析保留
        blocks = parse_srt_text(
            "1\n00:00:01,000 --> 00:00:02,000\nHello\n\n"
            "2\n00:00:03,000 --> 00:00:04,000\n\n"
            "3\n00:00:05,000 --> 00:00:06,000\nWorld\n",
        )
        self.assertEqual([b.text for b in blocks], ["Hello", "", "World"])
        # 块间缺空行（畸形输入）时，不能把下一块序号+时间戳吞进上一块文本
        blocks = parse_srt_text(
            "1\n00:00:01,000 --> 00:00:02,000\nHello\n"
            "2\n00:00:03,000 --> 00:00:04,000\nWorld\n",
        )
        self.assertEqual(len(blocks), 2)
        self.assertEqual([b.text for b in blocks], ["Hello", "World"])
        self.assertEqual([b.start for b in blocks], [1.0, 3.0])
        self.assertEqual([b.end for b in blocks], [2.0, 4.0])


class TestSplitSentences(unittest.TestCase):
    def test_cjk_english_and_empty(self):
        self.assertEqual(split_sentences("你好。世界！你好吗？"),
                         ["你好。", "世界！", "你好吗？"])
        self.assertEqual(split_sentences("Hello world. How are you? I am fine."),
                         ["Hello world.", "How are you?", "I am fine."])
        self.assertEqual(split_sentences(""), [])


class TestCacheKey(unittest.TestCase):
    def test_deterministic_and_distinguishing(self):
        # 确定性 + 长度
        a = sentence_cache_key("Hello", "m", True)
        self.assertEqual(a, sentence_cache_key("Hello", "m", True))
        self.assertEqual(len(a), 64)
        # 区分双语/纯译文模式
        self.assertNotEqual(sentence_cache_key("Hello", "m", True),
                            sentence_cache_key("Hello", "m", False))
        # 区分模型
        self.assertNotEqual(sentence_cache_key("Hello", "m1", True),
                            sentence_cache_key("Hello", "m2", True))


class TestToSimplified(unittest.TestCase):
    def test_idempotent_simple(self):
        self.assertEqual(to_simplified("你好"), "你好")


class TestHasChinese(unittest.TestCase):
    def test_detects_chinese(self):
        self.assertTrue(has_chinese("这是中文"))
        self.assertTrue(has_chinese("Hello\n世界"))
        self.assertFalse(has_chinese("This is English"))
        self.assertFalse(has_chinese("Hello\nWorld"))


class TestSafeStem(unittest.TestCase):
    def test_normal_and_truncated(self):
        self.assertEqual(safe_stem("movie.mp4"), "movie")
        self.assertLessEqual(len(safe_stem("x" * 200 + ".mp4")), 80)


class TestJsonAtomic(unittest.TestCase):
    def test_save_load_and_missing_default(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.json"
            save_json(p, {"a": 1})
            self.assertEqual(load_json(p, {}), {"a": 1})
            missing = Path(d) / "missing.json"
            self.assertEqual(load_json(missing, {"x": 9}), {"x": 9})

    def test_concurrent_save_no_corruption(self):
        """回归：多线程并发 save_json 同一文件 → 不抛异常、文件始终为合法 JSON"""
        import threading
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "shared.json"
            errors = []

            def worker(i):
                for _ in range(20):
                    try:
                        save_json(p, {"done": [f"file{i}"]})
                    except Exception as e:  # noqa: BLE001
                        errors.append(e)

            ts = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
            self.assertEqual(errors, [], f"并发保存不应抛异常: {errors}")
            data = load_json(p, {})
            self.assertEqual(len(data.get("done", [])), 1, "最终文件应为完整单条记录")


class TestJobDisplay(unittest.TestCase):
    def test_has_icon_and_name(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "v.mp4"
            f.write_text("x")
            disp = fmt_job_display(f)
            self.assertIn("v.mp4", disp)
            self.assertIn("🎬", disp)


class TestFindSubtitle(unittest.TestCase):
    def test_find_existing_and_match_video(self):
        """find_existing / match_video 基本行为 + 忽略 .partial.srt 断点"""
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            # 子目录隔离：find_existing 只认同名/带语言后缀字幕
            zh_dir = d / "zh"; zh_dir.mkdir()
            vid = zh_dir / "movie.mp4"
            vid.write_text("x")
            zh = zh_dir / "movie.zh.srt"
            zh.write_text("1\n00:00:01,000 --> 00:00:02,000\nhi\n")
            self.assertEqual(find_existing_subtitle(vid), zh)
            # match_video 从 .srt 反查视频
            mv_dir = d / "mv"; mv_dir.mkdir()
            mv = mv_dir / "movie.srt"
            mv.write_text("1\n00:00:01,000 --> 00:00:02,000\nhi\n")
            (mv_dir / "movie.mp4").write_text("x")
            self.assertEqual(match_video_for_subtitle(mv, mv_dir), mv_dir / "movie.mp4")
            # 含点号文件名（剧集类常见）：前缀匹配，不得在第一个点截断
            dot_dir = d / "dot"; dot_dir.mkdir()
            sub = dot_dir / "Mr.Robot.S01E01.720p.zh.srt"
            sub.write_text("1\n00:00:01,000 --> 00:00:02,000\nhi\n")
            (dot_dir / "Mr.Robot.S01E01.720p.mp4").write_text("x")
            self.assertEqual(match_video_for_subtitle(sub, dot_dir),
                             dot_dir / "Mr.Robot.S01E01.720p.mp4")
            # 精确同名优先于前缀匹配
            exact_dir = d / "exact"; exact_dir.mkdir()
            sub2 = exact_dir / "movie.zh.srt"
            sub2.write_text("1\n00:00:01,000 --> 00:00:02,000\nhi\n")
            (exact_dir / "movie.mp4").write_text("x")
            (exact_dir / "movie.zh.mp4").write_text("x")
            self.assertEqual(match_video_for_subtitle(sub2, exact_dir),
                             exact_dir / "movie.zh.mp4")
        # 崩溃遗留的 .partial.srt 断点不得被当作成品字幕
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            vid = d / "movie.mp4"
            vid.write_text("x")
            (d / "movie.partial.srt").write_text(
                "1\n00:00:01,000 --> 00:00:02,000\nhi\n")
            self.assertIsNone(find_existing_subtitle(vid))
            # 正常字幕仍能找到
            (d / "movie.zh.srt").write_text(
                "1\n00:00:01,000 --> 00:00:02,000\nhi\n")
            self.assertIsNotNone(find_existing_subtitle(vid))


class TestWrapSubtitleText(unittest.TestCase):
    """wrap_subtitle_text：长行折行，防止播放器中一行过长被挤压换行"""

    M = 25

    def test_short_and_disabled(self):
        """短行/空行不变；宽度 0 禁用折行"""
        self.assertEqual(wrap_subtitle_text("短句不折", self.M), "短句不折")
        self.assertEqual(wrap_subtitle_text("", self.M), "")
        long_line = "无折行" * 30
        self.assertEqual(wrap_subtitle_text(long_line, 0), long_line)

    def test_cjk_splits_at_punctuation_within_width(self):
        # 用户实际示例：34 字中文行应在标点附近折成两行，每行 <= 25
        t = "我该怎么办？今天在课堂上，连过去的黑历史都被翻出来，我根本无法上课。"
        lines = wrap_subtitle_text(t, self.M).split("\n")
        self.assertGreater(len(lines), 1)
        for l in lines:
            self.assertLessEqual(len(l), self.M)
        self.assertEqual("".join(lines), t)
        # 无标点长串硬切，仍保持每行宽度
        t2 = "あ" * 80
        lines2 = wrap_subtitle_text(t2, self.M).split("\n")
        self.assertTrue(all(len(l) <= self.M for l in lines2))
        self.assertEqual("".join(lines2), t2)

    def test_bilingual_and_latin_wrap(self):
        """双语块各行独立折行；拉丁文本按词边界折不断词"""
        src = "どうしたらいいんだ。今日クラスで黒歴史まで垂らされて授業何もできなかった。"
        zh = "我该怎么办？今天在课堂上，连过去的黑历史都被翻出来，我根本无法上课。"
        out = wrap_subtitle_text(src + "\n" + zh, self.M)
        for l in out.split("\n"):
            self.assertLessEqual(len(l), self.M)
        self.assertIn("\n", out)
        # 拉丁文本
        t = "This is a fairly long English subtitle line that should be wrapped nicely"
        lines = wrap_subtitle_text(t, self.M).split("\n")
        self.assertTrue(all(len(l) <= self.M for l in lines))
        self.assertEqual(" ".join(lines), t)
        # 不在单词中间切断
        for l in lines:
            self.assertNotIn("  ", l)

    def test_write_srt_applies_wrapping(self):
        long_text = "今天在课堂上连过去的黑历史都被翻出来了，我根本没办法专心上课，太难受了。"
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "w.srt"
            blocks = [SubtitleBlock(index=1, start=1.0, end=3.0, text=long_text)]
            write_srt(out, blocks, [long_text])
            content = out.read_text(encoding="utf-8")
            self.assertNotIn(long_text, content)  # 原文长行已被折行
            re = parse_srt(out)
            self.assertEqual(re[0].text.replace("\n", ""), long_text)


if __name__ == "__main__":
    unittest.main()
