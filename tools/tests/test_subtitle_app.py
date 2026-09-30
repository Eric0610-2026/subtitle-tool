#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""入口依赖安装标记的回归测试。"""
import tempfile
import unittest
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

from subtitle_app import subtitle_app
from subtitle_app import qt_app


class ModelPathMigrationTest(unittest.TestCase):
    def test_relative_model_uses_project_root_from_other_working_directory(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "project with spaces"
            root.mkdir()
            previous = Path.cwd()
            try:
                os.chdir(d)
                with patch.object(qt_app, "APP_DIR", root):
                    self.assertEqual(
                        qt_app._resolve_model_dir("models/whisper"),
                        str(root / "models" / "whisper"),
                    )
            finally:
                os.chdir(previous)

    def test_saved_internal_model_follows_moved_project(self):
        with tempfile.TemporaryDirectory() as d:
            old_root = Path(d) / "old project"
            new_root = Path(d) / "new project"
            with patch.object(qt_app, "APP_DIR", old_root):
                saved = qt_app._portable_model_dir(str(old_root / "models" / "whisper"))
            with patch.object(qt_app, "APP_DIR", new_root):
                self.assertEqual(qt_app._resolve_model_dir(saved), str(new_root / "models" / "whisper"))

    def test_external_model_is_preserved(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "project"
            external = Path(d) / "shared models" / "whisper"
            with patch.object(qt_app, "APP_DIR", root):
                self.assertEqual(qt_app._portable_model_dir(str(external)), str(external))

    def test_empty_model_stays_empty(self):
        self.assertEqual(qt_app._resolve_model_dir(""), "")
        self.assertEqual(qt_app._portable_model_dir(""), "")


class DependencyInstallTest(unittest.TestCase):
    def test_marker_tracks_requirements_and_interpreter(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            tools = root / "tools"
            tools.mkdir()
            req = tools / "requirements.txt"
            req.write_text("example>=1\n", encoding="utf-8")
            marker = root / "cache" / ".deps_installed"
            run = MagicMock(return_value=MagicMock(returncode=0))
            with patch.object(subtitle_app, "_APP_DIR", root), \
                    patch.object(subtitle_app, "_MARKER", marker), \
                    patch.object(subtitle_app.sys, "executable", str(root / "python.exe")), \
                    patch.object(subtitle_app.subprocess, "run", run):
                self.assertTrue(subtitle_app._ensure_deps())
                self.assertEqual(run.call_count, 1)
                self.assertTrue(subtitle_app._ensure_deps())
                self.assertEqual(run.call_count, 1)

                req.write_text("example>=2\n", encoding="utf-8")
                self.assertTrue(subtitle_app._ensure_deps())
                self.assertEqual(run.call_count, 2)

                with patch.object(subtitle_app.sys, "executable", str(root / "other-python.exe")):
                    self.assertTrue(subtitle_app._ensure_deps())
                self.assertEqual(run.call_count, 3)

                marker.write_text("", encoding="utf-8")  # 旧版标记
                self.assertTrue(subtitle_app._ensure_deps())
                self.assertEqual(run.call_count, 4)

    def test_failed_install_does_not_create_marker(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "tools").mkdir()
            (root / "tools" / "requirements.txt").write_text("example\n", encoding="utf-8")
            marker = root / "cache" / ".deps_installed"
            with patch.object(subtitle_app, "_APP_DIR", root), \
                    patch.object(subtitle_app, "_MARKER", marker), \
                    patch.object(subtitle_app.subprocess, "run", return_value=MagicMock(returncode=1, stderr="failed")), \
                    patch.object(subtitle_app, "_msgbox"):
                self.assertFalse(subtitle_app._ensure_deps())
            self.assertFalse(marker.exists())
