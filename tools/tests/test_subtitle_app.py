#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""入口依赖安装标记的回归测试。"""
import tempfile
import unittest
import os
import json
import shutil
import subprocess
import sys
import time
import venv
import ctypes
from ctypes import wintypes
from pathlib import Path
from unittest.mock import MagicMock, patch

from subtitle_app import subtitle_app
from subtitle_app import qt_app


@unittest.skipUnless(sys.platform == "win32", "Windows launchers")
class PortableLauncherTest(unittest.TestCase):
    def test_exe_survives_move_and_finds_python(self):
        self._check_launcher("字幕工具.exe")

    def _rename_after_exit(self, source, target):
        # Windows 虚拟环境的转发进程可能比探针本身稍晚退出。
        deadline = time.monotonic() + 10
        while True:
            try:
                return source.rename(target)
            except PermissionError as error:
                if error.winerror != 32 or time.monotonic() >= deadline:
                    raise
                time.sleep(0.05)

    def _check_launcher(self, filename):
        project = Path(__file__).resolve().parents[2]
        launcher = project / filename
        with tempfile.TemporaryDirectory() as d:
            parent = Path(d)
            root = parent / "原目录 with spaces & parentheses (1)"
            (root / "subtitle_app").mkdir(parents=True)
            shutil.copy2(launcher, root / launcher.name)
            (root / "subtitle_app" / "subtitle_app.py").write_text(
                "import json, os, sys\nfrom pathlib import Path\n"
                "root = Path(__file__).resolve().parents[1]\n"
                "visible = None\n"
                "if os.environ.get('LAUNCHER_GUI_CHECK') == '1':\n"
                "    import ctypes\n"
                "    from PySide6.QtWidgets import QApplication, QWidget\n"
                "    app = QApplication([])\n"
                "    window = QWidget()\n"
                "    window.setWindowTitle('Subtitle launcher test')\n"
                "    window.resize(100, 80)\n"
                "    window.show()\n"
                "    app.processEvents()\n"
                "    visible = bool(ctypes.windll.user32.IsWindowVisible(ctypes.c_void_p(int(window.winId()))))\n"
                "    window.close()\n"
                "result = root / 'launch-result.json'\n"
                "temporary = result.with_suffix('.tmp')\n"
                "temporary.write_text(json.dumps({'cwd': os.getcwd(), "
                "'python': sys.executable, 'entry': sys.argv[0], 'pid': os.getpid(), 'visible': visible}), encoding='utf-8')\n"
                "temporary.replace(result)\n", encoding="utf-8",
            )
            venv.EnvBuilder(with_pip=False).create(root / ".venv")
            env = dict(os.environ)
            env["PATH"] = str(Path(sys.executable).parent)
            env["QT_QPA_PLATFORM"] = "windows"
            env.pop("LAUNCHER_GUI_CHECK", None)
            for stage in ("original", "moved", "system-python"):
                with self.subTest(stage=stage):
                    if stage == "moved":
                        root = self._rename_after_exit(root, parent / "移动后的目录 with spaces & (2)")
                    elif stage == "system-python":
                        self._rename_after_exit(root / ".venv", root / "unused-env")
                        env["LAUNCHER_GUI_CHECK"] = "1"
                    result = root / "launch-result.json"
                    result.unlink(missing_ok=True)
                    completed = subprocess.run(
                        [str(root / launcher.name)],
                        cwd=parent, env=env, capture_output=True, timeout=10,
                    )
                    self.assertEqual(completed.returncode, 0, completed.stderr)
                    deadline = time.monotonic() + 10
                    while not result.exists() and time.monotonic() < deadline:
                        time.sleep(0.05)
                    self.assertTrue(result.is_file(), "Launcher did not launch Python")
                    data = json.loads(result.read_text(encoding="utf-8"))
                    # 探针写完结果后仍可能正在退出；等待其释放目录，再模拟移动。
                    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
                    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
                    kernel.OpenProcess.restype = wintypes.HANDLE
                    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
                    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
                    process = kernel.OpenProcess(0x00100000, False, data["pid"])
                    if process:
                        try:
                            self.assertEqual(kernel.WaitForSingleObject(process, 10000), 0)
                        finally:
                            kernel.CloseHandle(process)
                    self.assertEqual(Path(data["cwd"]), root)
                    self.assertEqual(Path(data["entry"]), root / "subtitle_app/subtitle_app.py")
                    expected = (Path(sys.executable).with_name("pythonw.exe")
                                if stage == "system-python" else root / ".venv/Scripts/pythonw.exe")
                    self.assertEqual(Path(data["python"]), expected)
                    if stage == "system-python":
                        self.assertTrue(data["visible"], "Launcher hid the native Qt window")


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
