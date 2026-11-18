#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P0/P1 regressions, with isolated files and mocked model/ffmpeg calls."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from subtitle_app import electron_backend, translation
from subtitle_app.srt_utils import SubtitleBlock, load_json, save_json, sanitize_blocks, sentence_cache_key
from subtitle_app.translator import translate_only


def setUpModule():
    directory = tempfile.TemporaryDirectory(prefix="subtitle_test_backup_")
    unittest.addModuleCleanup(directory.cleanup)
    backup_patch = patch("subtitle_app.translator._BACKUP_DIR", Path(directory.name))
    backup_patch.start()
    unittest.addModuleCleanup(backup_patch.stop)


class TestDesktopRegressions(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        root_patch = patch.object(electron_backend, "ROOT", self.root)
        root_patch.start()
        self.addCleanup(root_patch.stop)
        self.events = []
        self.backend = electron_backend.DesktopBackend(lambda message: self.events.append(message["event"]))

    def test_loads_nonempty_ignore_and_migrates_legacy_progress(self):
        path = self.root / "movie.mp4"
        path.touch()
        save_json(self.root / ".subtitle_progress.json", {"ignored": [str(path)]})
        backend = electron_backend.DesktopBackend(Mock())
        backend.add([str(path)])
        self.assertTrue(backend.history_path.exists())
        self.assertTrue(backend.queue()[0]["ignored"])

    def test_media_queue_continues_with_frozen_settings_and_reports_failure(self):
        backend = self.backend
        first, second, third = (self.root / f"{i}.mp4" for i in (1, 2, 3))
        subtitle = self.root / "1.srt"
        for path in (first, second, third, subtitle):
            path.touch()
        backend.add([str(first)])
        backend.settings["target_lang"] = "zh"
        backend.settings["name_glossaries"] = {"zh": {"ぬね": "甲名"}}
        batches = []

        def run_batch(jobs, opts):
            batches.append((list(jobs), opts["target_lang"], opts["name_glossaries"]))
            if jobs == [first]:
                backend.save_settings({"target_lang": "ja", "glossary_texts": {"zh": "ぬね=丙名"}}, False)
                backend.add([str(subtitle), str(second), str(third)], check_done=False)
                backend.change_queue("remove", list(map(str, (first, second, third))))
                self.assertEqual(backend.jobs, [first])
                backend.add([str(second)])
                opts["post"]({"type": "error", "message": "第一批失败"})
            elif jobs == [second]:
                backend.add([str(third)])
                opts["post"]({"type": "done"})
            else:
                opts["post"]({"type": "done"})

        with patch.object(backend, "launch", side_effect=lambda _kind, run, *args: run(*args)), \
                patch.object(backend.worker, "start", side_effect=run_batch), \
                patch.object(backend.worker, "thread", Mock()):
            backend.start_processing()
        self.assertEqual(batches, [([path], "zh", {"zh": {"ぬね": "甲名"}}) for path in (first, second, third)])
        self.assertTrue(any(event.get("message") == "第一批失败" for event in self.events))
        self.assertTrue(self.events[-1]["failed"])

    def test_stop_prevents_next_batch_and_preserves_waiting_files(self):
        backend = self.backend
        first, waiting = self.root / "first.mp4", self.root / "waiting.mp4"
        first.touch()
        waiting.touch()
        backend.add([str(first)])

        def run_batch(jobs, opts):
            backend.add([str(waiting)])
            backend.stop()
            opts["post"]({"type": "done", "stopped": True})

        with patch.object(backend, "launch", side_effect=lambda _kind, run, *args: run(*args)), \
                patch.object(backend.worker, "start", side_effect=run_batch) as start, \
                patch.object(backend.worker, "thread", Mock()):
            backend.start_processing()
        start.assert_called_once()
        self.assertEqual(backend.jobs, [first, waiting])
        self.assertTrue(self.events[-1]["stopped"])

    def test_completed_history_matches_full_path_only(self):
        first, second = self.root / "目录 A", self.root / "目录 B"
        first.mkdir()
        second.mkdir()
        completed = first / "movie.part1.mp4"
        remaining = [second / completed.name, first / "movie.mp4", first / "movie.part1.wav"]
        for path in [completed, *remaining]:
            path.touch()
        recorded = str(completed)
        if os.name == "nt":
            recorded = recorded.upper().replace("\\", "/")
        save_json(self.backend.history_path, {"done": [recorded]})
        self.backend.add([str(first), str(second)])
        self.assertCountEqual(self.backend.jobs, remaining)
        self.backend.add([str(completed)], check_done=False)
        self.assertCountEqual(self.backend.jobs, [*remaining, completed])

    def test_restoring_ignored_file_updates_queue_and_history(self):
        path = self.root / "movie.mp4"
        path.touch()
        self.backend.add([str(path)])
        self.backend.change_queue("ignore", [str(path)])
        self.assertTrue(self.backend.queue()[0]["ignored"])
        self.backend.remove_history("ignored", [str(path)])
        self.assertEqual(self.backend.history()["ignored"], [])
        self.assertFalse(self.backend.queue()[0]["ignored"])

    def test_cache_deletes_one_before_first_translation(self):
        path = self.root / electron_backend.CACHE
        save_json(path, {"a": "one", "b": "two", "c": "three"})
        with patch.object(translation, "_shared_cache", {}), patch.object(translation, "_shared_cache_path", None):
            self.assertEqual(dict(self.backend.cache(["a"])), {"b": "two", "c": "three"})
        self.assertEqual(load_json(path), {"b": "two", "c": "three"})

    def test_manual_embed_preserves_untrusted_sources(self):
        video, subtitle, mkv = (self.root / f"test.{ext}" for ext in ("mp4", "srt", "mkv"))
        for path in (video, subtitle, mkv):
            path.touch()
        with patch.object(electron_backend.muxer, "embed_subtitles_to_video", return_value=(mkv, False)):
            self.backend.process_media("embed", [(video, subtitle)], "ffmpeg", False)
        self.assertTrue(video.exists())
        self.assertTrue(subtitle.exists())
        self.assertTrue(self.events[-1]["failed"])


class TestTranslationRegressions(unittest.TestCase):
    def test_short_cues_do_not_accumulate_drift(self):
        blocks = [SubtitleBlock(i + 1, i / 10, (i + 1) / 10, "short") for i in range(100)]
        sanitize_blocks(blocks)
        self.assertLessEqual(blocks[-1].end, 10.5)
        self.assertEqual([b.start for b in blocks], [i / 10 for i in range(100)])
        self.assertTrue(all(b.end > b.start for b in blocks))

    def test_file_failure_does_not_emit_global_error(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            srt = root / "movie.srt"
            srt.write_text("1\n00:00:00,000 --> 00:00:01,000\nHello", encoding="utf-8")
            events = []
            with patch("subtitle_app.translator.ensure_running", return_value=(True, "ok", False)), patch("subtitle_app.translator.TranslationClient") as cls:
                cls.return_value.translate_blocks.side_effect = RuntimeError("bad batch")
                with self.assertRaises(RuntimeError):
                    translate_only(srt, root, srt, 1, 1, {"work_dir": directory, "language": "en", "translate_enabled": True, "_detected_lang": "en"}, events.append)
            self.assertNotIn("error", [event["type"] for event in events])

    def test_stop_during_missing_translation_saves_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = translation.TranslationClient(root / "cache.json", lambda e: None)
            state = root / "state.json"
            stopped = False
            def batch(texts, *args):
                nonlocal stopped
                if len(texts) == 1:
                    stopped = True
                    return [{"id": 1, "zh": "translated"}]
                return [{"id": 1, "zh": "translated first"}]
            blocks = [SubtitleBlock(i, i, i + 1, f"sentence {i}") for i in range(3)]
            with patch.object(client, "_translate_batch", side_effect=batch):
                with self.assertRaises(translation.TranslationStopped):
                    client.translate_blocks(blocks, "en", True, state, stop_check=lambda: stopped)
            saved = load_json(state, {})
            self.assertEqual(saved["target_lang"], "zh")
            self.assertTrue(saved["done"])

    def test_target_language_separates_cache_and_state(self):
        self.assertNotEqual(sentence_cache_key("Hello", "m", True, "zh"), sentence_cache_key("Hello", "m", True, "en"))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = translation.TranslationClient(root / "cache.json", lambda e: None, target_lang="en")
            state = root / "state.json"
            save_json(state, {"target_lang": "zh", "originals": {"0": "Hello"}, "done": {"0": "旧译文"}})
            blocks = [SubtitleBlock(1, 0, 1, "Hello")]
            with patch.object(client, "_translate_batch", return_value=[{"id": 1, "zh": "new translation"}]) as call:
                result = client.translate_blocks(blocks, "fr", True, state)
            call.assert_called_once()
            self.assertEqual(result, ["new translation"])
            self.assertIn(sentence_cache_key("Hello", client.model, True, "en"), client.cache)
            self.assertEqual(load_json(state, {})["target_lang"], "en")
