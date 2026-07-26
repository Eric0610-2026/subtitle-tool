#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""srt_utils 单元测试（适配当前代码）"""
import unittest
import tempfile
from pathlib import Path

from subtitle_app.srt_utils import (
    seconds_to_srt_time, srt_time_to_seconds, fmt_duration,
    SubtitleBlock, parse_srt, write_srt, split_sentences,
    sentence_cache_key, to_simplified, has_chinese, safe_stem,
    load_json, save_json, fmt_job_display, find_existing_subtitle,
    match_video_for_subtitle, VIDEO_EXTS, SUB_EXTS,
    analyze_subtitle_quality, analyze_subtitle_file, format_quality_report,
)


class TestTimeUtils(unittest.TestCase):
    def test_seconds_to_srt_time_basic(self):
        self.assertEqual(seconds_to_srt_time(0), "00:00:00,000")
        self.assertEqual(seconds_to_srt_time(3661.5), "01:01:01,500")

    def test_srt_time_to_seconds_roundtrip(self):
        for sec in (0, 1.0, 59.999, 3661.5, 7200.123):
            self.assertAlmostEqual(srt_time_to_seconds(seconds_to_srt_time(sec)), sec, places=2)

    def test_fmt_duration(self):
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

    def test_parse_srt(self):
        with tempfile.TemporaryDirectory() as d:
            blocks = parse_srt(self._sample(Path(d)))
            self.assertEqual(len(blocks), 2)
            self.assertEqual(blocks[0].text, "你好世界")
            self.assertAlmostEqual(blocks[0].start, 1.0)
            self.assertAlmostEqual(blocks[0].end, 3.0)

    def test_write_srt_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            blocks = parse_srt(self._sample(Path(d)))
            out = Path(d) / "out.srt"
            write_srt(out, blocks, [b.text for b in blocks])
            re = parse_srt(out)
            self.assertEqual(len(re), 2)
            self.assertEqual(re[1].text, "第二句")

    def test_subtitle_block_timing(self):
        b = SubtitleBlock(index=1, start=1.0, end=3.0, text="x")
        self.assertEqual(b.timing, "00:00:01,000 --> 00:00:03,000")


class TestSplitSentences(unittest.TestCase):
    def test_chinese(self):
        self.assertEqual(split_sentences("你好。世界！你好吗？"), ["你好。", "世界！", "你好吗？"])

    def test_english_period(self):
        self.assertEqual(split_sentences("Hello world. How are you? I am fine."),
                         ["Hello world.", "How are you?", "I am fine."])

    def test_empty(self):
        self.assertEqual(split_sentences(""), [])


class TestCacheKey(unittest.TestCase):
    def test_deterministic(self):
        a = sentence_cache_key("Hello", "m", True)
        b = sentence_cache_key("Hello", "m", True)
        self.assertEqual(a, b)
        self.assertEqual(len(a), 64)

    def test_distinguishes_mode(self):
        self.assertNotEqual(sentence_cache_key("Hello", "m", True),
                            sentence_cache_key("Hello", "m", False))

    def test_distinguishes_model(self):
        self.assertNotEqual(sentence_cache_key("Hello", "m1", True),
                            sentence_cache_key("Hello", "m2", True))


class TestToSimplified(unittest.TestCase):
    def test_idempotent_simple(self):
        self.assertEqual(to_simplified("你好"), "你好")


class TestHasChinese(unittest.TestCase):
    def test_true(self):
        self.assertTrue(has_chinese("这是中文"))
        self.assertTrue(has_chinese("Hello\n世界"))

    def test_false(self):
        self.assertFalse(has_chinese("This is English"))
        self.assertFalse(has_chinese("Hello\nWorld"))


class TestSafeStem(unittest.TestCase):
    def test_truncates(self):
        self.assertLessEqual(len(safe_stem("x" * 200 + ".mp4")), 80)

    def test_normal(self):
        self.assertEqual(safe_stem("movie.mp4"), "movie")


class TestJsonAtomic(unittest.TestCase):
    def test_save_load(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.json"
            save_json(p, {"a": 1})
            self.assertEqual(load_json(p, {}), {"a": 1})

    def test_default(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "missing.json"
            self.assertEqual(load_json(p, {"x": 9}), {"x": 9})


class TestJobDisplay(unittest.TestCase):
    def test_has_icon_and_name(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "v.mp4"
            f.write_text("x")
            disp = fmt_job_display(f)
            self.assertIn("v.mp4", disp)
            self.assertIn("🎬", disp)


class TestFindSubtitle(unittest.TestCase):
    def test_find_existing(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            vid = d / "movie.mp4"
            vid.write_text("x")
            sub = d / "movie.zh.srt"
            sub.write_text("1\n00:00:01,000 --> 00:00:02,000\nhi\n")
            self.assertEqual(find_existing_subtitle(vid), sub)

    def test_match_video(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            vid = d / "movie.mp4"
            vid.write_text("x")
            sub = d / "movie.srt"
            sub.write_text("1\n00:00:01,000 --> 00:00:02,000\nhi\n")
            self.assertEqual(match_video_for_subtitle(sub, d), vid)


class TestAnalyzeSubtitleQuality(unittest.TestCase):
    """质量报告：计数 + 样例，覆盖空条/过长/间隙等硬伤"""

    def test_clean_blocks_zero_issues(self):
        blocks = [
            SubtitleBlock(1, 0.0, 2.0, "Hello"),
            SubtitleBlock(2, 2.5, 4.0, "World"),
        ]
        report = analyze_subtitle_quality(blocks)
        self.assertEqual(report["total_issues"], 0)
        self.assertEqual(report["total_cues"], 2)
        self.assertTrue(all(v == 0 for v in report["counts"].values()))
        lines = format_quality_report(report)
        self.assertTrue(any("通过" in ln for ln in lines))

    def test_detects_empty_long_gap_overlap(self):
        blocks = [
            SubtitleBlock(1, 0.0, 20.0, "long cue"),          # too_long
            SubtitleBlock(2, 20.0, 21.0, "   "),              # empty
            SubtitleBlock(3, 40.0, 41.0, "after gap"),        # gap > 10
            SubtitleBlock(4, 40.5, 41.5, "overlap"),          # overlap with #3
            SubtitleBlock(5, 42.0, 42.1, "tiny"),             # too_short
        ]
        report = analyze_subtitle_quality(blocks, sample_limit=10)
        c = report["counts"]
        self.assertGreaterEqual(c["too_long"], 1)
        self.assertGreaterEqual(c["empty"], 1)
        self.assertGreaterEqual(c["gap"], 1)
        self.assertGreaterEqual(c["overlap"], 1)
        self.assertGreaterEqual(c["too_short"], 1)
        self.assertGreater(report["total_issues"], 0)
        self.assertTrue(report["samples"])
        lines = format_quality_report(report)
        self.assertTrue(any("质量提醒" in ln for ln in lines))

    def test_analyze_subtitle_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "q.srt"
            # 人工写入含空文本的块：parse 后 text 为空字符串
            p.write_text(
                "1\n00:00:00,000 --> 00:00:20,000\nlong text here\n\n"
                "2\n00:00:40,000 --> 00:00:41,000\n\n",
                encoding="utf-8",
            )
            report = analyze_subtitle_file(p)
            self.assertIsNotNone(report)
            self.assertEqual(report["name"], "q.srt")
            self.assertGreaterEqual(report["counts"]["too_long"], 1)
            # 第 2 条 content 为空 → empty
            self.assertGreaterEqual(report["counts"]["empty"], 1)
            self.assertGreaterEqual(report["counts"]["gap"], 1)


if __name__ == "__main__":
    unittest.main()
