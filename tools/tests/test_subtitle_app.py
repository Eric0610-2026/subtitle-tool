#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""入口依赖安装标记的回归测试。"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from subtitle_app import subtitle_app


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
