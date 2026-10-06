#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P0/P1 regressions, with isolated files and mocked model/ffmpeg calls."""
import os
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QAbstractButton, QListWidgetItem, QMessageBox, QDialog, QListWidget, QPushButton
from subtitle_app import qt_app, translation, dialogs
from subtitle_app.panels import EditDialog
from subtitle_app.srt_utils import SubtitleBlock, has_chinese, load_json, save_json, sanitize_blocks, sentence_cache_key
from subtitle_app.translator import translate_only


def setUpModule():
    directory = tempfile.TemporaryDirectory(prefix="subtitle_test_backup_")
    unittest.addModuleCleanup(directory.cleanup)
    backup_patch = patch("subtitle_app.translator._BACKUP_DIR", Path(directory.name))
    backup_patch.start()
    unittest.addModuleCleanup(backup_patch.stop)


class TestWindowRegressions(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.directory = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.root = Path(self.directory)
        self.stack.enter_context(patch.object(qt_app, "APP_DIR", self.root))
        for method in ("_start_handoff_server", "_restore_window_state",
                       "_save_window_state", "_run_startup_checks", "_setup_tray"):
            self.stack.enter_context(patch.object(qt_app.SubtitleApp, method))

    def make_window(self):
        window = qt_app.SubtitleApp()
        window._tray_icon = None
        def close():
            window._exiting = True
            with patch.object(window.worker, "stop"), patch("subtitle_app.local_service.shutdown_service", return_value=(True, "Hy-MT2 已卸下")):
                window.close()
        self.addCleanup(close)
        return window

    def test_start_with_nonempty_ignore(self):
        save_json(self.root / qt_app.IGNORE_FILE, {"ignored": ["example.mp4"]})
        self.assertEqual(self.make_window()._ignore_set, {"example.mp4"})

    def test_main_and_settings_button_cursors_in_both_themes(self):
        window = self.make_window()
        window.show()
        dialog = dialogs.SettingsDialog(window, {})
        self.addCleanup(dialog.close)
        dialog.show()
        for dark in (False, True):
            window.dark_mode = dark
            window.colors = qt_app.DARK if dark else qt_app.LIGHT
            window._apply_style()
            self.app.processEvents()
            self.assertFalse(window.grab().isNull())
            self.assertFalse(dialog.grab().isNull())
            buttons = window.findChildren(QAbstractButton)
            self.assertTrue(buttons)
            self.assertTrue(dialog.findChildren(QAbstractButton))
            for button in buttons:
                expected = Qt.PointingHandCursor if button.isEnabled() else Qt.ArrowCursor
                self.assertEqual(button.cursor().shape(), expected, button.text())

    def test_start_with_legacy_progress(self):
        save_json(self.root / ".subtitle_progress.json", {"done": ["old.mp4"]})
        self.make_window()
        self.assertTrue((self.root / qt_app.IGNORE_FILE).exists())

    def test_progress_keeps_updating_while_preview_is_frozen(self):
        window = self.make_window()
        original = "\n\n".join(f"{i}\n00:00:01,000 --> 00:00:02,000\noriginal {i}" for i in range(1, 101))
        window._handle_preview_append({"message": original})
        window.preview_panel._flush_live_render()
        window._handle_preview_live({"message": original.replace("original", "final")})
        self.assertEqual(window.preview_panel.preview.item(0, 2).text(), "original 1")
        self.assertIn("final 100", window.preview_panel.get_text())
        window._handle_progress({"stage": "转写中", "percent": 65, "detail": "650 段"})
        self.assertEqual(window.progress_panel.stage_bar.value(), 65)
        window._handle_progress({"stage": "翻译", "percent": 83, "detail": "批次 83/100 完成"})
        self.assertEqual(window.progress_panel.stage_bar.value(), 83)
        self.assertEqual(window.progress_panel.stage_detail.text(), "批次 83/100 完成")

    def test_listen_failure_retains_lock(self):
        with patch.object(qt_app, "QLockFile") as lock_cls, patch.object(qt_app, "QLocalServer") as server_cls:
            lock_cls.return_value.tryLock.return_value = True
            server_cls.return_value.listen.return_value = False
            lock, server = qt_app._claim_single_instance(Mock())
            self.assertIs(lock, lock_cls.return_value)
            self.assertIsNone(server)
            lock.unlock.assert_not_called()

    def test_manual_embed_preserves_untrusted_sources(self):
        window = self.make_window()
        video, srt, mkv = [self.root / name for name in ("movie.mp4", "movie.srt", "movie.mkv")]
        for path in (video, srt, mkv):
            path.write_text("original", encoding="utf-8")
        with patch.object(qt_app, "embed_subtitles_to_video", return_value=(mkv, False)), patch.object(window.signal_bridge, "post") as post:
            window._manual_embed_worker([(video, srt)], "ffmpeg")
        self.assertTrue(video.exists())
        self.assertTrue(srt.exists())
        self.assertEqual(post.call_args.args[0]["success"], 0)
        self.assertNotIn(str(mkv.resolve()), window._output_paths)

    def test_settings_saved_and_read_at_start(self):
        config_path = self.root / "config.json"
        save_json(config_path, {})
        with patch.object(qt_app.cfg.translation, "translation_only", True, create=True), patch.object(qt_app.cfg.translation, "send_all", True, create=True):
            window = self.make_window()
        self.assertTrue(window.settings_data["translation_only"])
        self.assertTrue(window.settings_data["send_all"])
        with patch.object(qt_app, "__file__", str(self.root / "qt_app.py")):
            window._save_settings_permanently({"translation_only": True, "send_all": True})
        self.assertTrue(load_json(config_path, {})["translation"]["translation_only"])
        self.assertTrue(load_json(config_path, {})["translation"]["send_all"])

    def test_history_restore_reload_and_visuals(self):
        ignored_path = self.root / "movie.mp4"
        ignored = str(ignored_path.resolve())
        save_json(self.root / qt_app.IGNORE_FILE, {"ignored": [ignored]})
        window = self.make_window()
        item = QListWidgetItem("movie.mp4")
        item.setData(Qt.UserRole, ignored)
        window.video_list.addItem(item)
        window._refresh_item_visual(item)
        self.assertTrue(item.font().strikeOut())
        with patch.object(qt_app, "show_history_dialog", side_effect=lambda *a: save_json(window._ignore_path, {"ignored": []})):
            window._show_history()
        self.assertFalse(window._is_ignored(ignored_path))
        self.assertFalse(item.font().strikeOut())
        window._ignore_set.add("another.mp4")
        window._save_ignore()
        self.assertNotIn(ignored, load_json(window._ignore_path, {})["ignored"])

    def test_repaginate_preserves_buffer_and_earlier_edits(self):
        text = "\n\n".join(f"{i}\n00:00:00,000 --> 00:00:01,000\nline {i}" for i in range(1, 31))
        dialog = EditDialog(text)
        self.addCleanup(dialog.close)
        dialog._editor.setPlainText(dialog._editor.toPlainText().replace("line 1\n", "changed 1\n"))
        dialog._next_page()
        dialog._editor.setPlainText(dialog._editor.toPlainText().replace("line 11", "changed 11"))
        dialog._on_page_size_changed("20")
        merged = dialog.get_merged_text()
        self.assertEqual(len(merged.split("\n\n")), 30)
        self.assertIn("changed 1", merged)
        self.assertIn("changed 11", merged)
        dialog._on_page_size_changed("全部")
        self.assertEqual(dialog.get_merged_text(), merged)

    def test_cache_dialog_deletes_one_before_first_translation(self):
        path = self.root / "cache" / ".subtitle_translation_cache.json"
        save_json(path, {"a": "one", "b": "two", "c": "three"})
        def interact(dialog):
            if isinstance(dialog, QMessageBox):
                return QMessageBox.Yes
            listing = dialog.findChild(QListWidget)
            listing.item(0).setSelected(True)
            for button in dialog.findChildren(QPushButton):
                if "删除选中" in button.text():
                    button.click()
            return QDialog.Accepted
        with patch.object(translation, "_shared_cache", {}), patch.object(translation, "_shared_cache_path", None), patch.object(QDialog, "exec", interact):
            dialogs.show_cache_dialog(None, str(self.root), Mock())
        self.assertEqual(load_json(path, {}), {"b": "two", "c": "three"})


class TestTranslationRegressions(unittest.TestCase):
    def test_short_cues_do_not_accumulate_drift(self):
        blocks = [SubtitleBlock(i + 1, i / 10, (i + 1) / 10, "short") for i in range(100)]
        sanitize_blocks(blocks)
        self.assertLessEqual(blocks[-1].end, 10.5)
        self.assertEqual([b.start for b in blocks], [i / 10 for i in range(100)])
        self.assertTrue(all(b.end > b.start for b in blocks))

    def test_multiline_japanese_is_not_chinese(self):
        self.assertFalse(has_chinese("こんにちは\n世界", "ja"))
        self.assertFalse(has_chinese("Hello\n世界です"))
        self.assertTrue(has_chinese("Hello\n世界"))

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
