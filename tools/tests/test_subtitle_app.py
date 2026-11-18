#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Windows 启动器与后端配置的回归测试。"""
import tempfile
import unittest
import os
import json
import shutil
import subprocess
import sys
import time
import ctypes
from ctypes import wintypes
from pathlib import Path
from unittest.mock import Mock, patch

from subtitle_app import electron_backend


@unittest.skipUnless(sys.platform == "win32", "Windows launchers")
class PortableLauncherTest(unittest.TestCase):
    def test_exe_survives_move_and_finds_electron(self):
        self._check_launcher("字幕工具.exe")

    def _rename_after_exit(self, source, target):
        # Windows 进程可能比探针写入结果稍晚释放目录。
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
        framework = Path(os.environ["WINDIR"]) / "Microsoft.NET"
        compiler = framework / "Framework64/v4.0.30319/csc.exe"
        if not compiler.exists():
            compiler = framework / "Framework/v4.0.30319/csc.exe"
        with tempfile.TemporaryDirectory() as d:
            parent = Path(d)
            root = parent / "原目录 with spaces & parentheses (1)"
            runtime = root / "node_modules/electron/dist"
            runtime.mkdir(parents=True)
            shutil.copy2(launcher, root / launcher.name)
            (root / "package.json").write_text("{}", encoding="utf-8")
            source = parent / "probe.cs"
            source.write_text(
                'using System; using System.Diagnostics; using System.IO; using System.Windows.Forms;\n'
                'class Probe { static void Main(string[] args) {\n'
                'using (var window = new Form()) {\n'
                'window.Show(); Application.DoEvents();\n'
                'File.WriteAllLines("launch-result.tmp", new [] { Environment.CurrentDirectory,\n'
                'AppDomain.CurrentDomain.BaseDirectory, String.Join(" ", args),\n'
                'Process.GetCurrentProcess().Id.ToString(), window.Visible.ToString() });\n'
                'File.Move("launch-result.tmp", "launch-result.txt");\n'
                '} } }', encoding="utf-8",
            )
            subprocess.run(
                [str(compiler), "/nologo", "/target:winexe", "/reference:System.Windows.Forms.dll",
                 f"/out:{runtime / 'electron.exe'}", str(source)],
                check=True, capture_output=True, timeout=20,
            )
            for stage in ("original", "moved"):
                with self.subTest(stage=stage):
                    if stage == "moved":
                        root = self._rename_after_exit(root, parent / "移动后的目录 with spaces & (2)")
                    result = root / "launch-result.txt"
                    result.unlink(missing_ok=True)
                    completed = subprocess.run(
                        [str(root / launcher.name)],
                        cwd=parent, capture_output=True, timeout=10,
                    )
                    self.assertEqual(completed.returncode, 0, completed.stderr)
                    deadline = time.monotonic() + 10
                    while not result.exists() and time.monotonic() < deadline:
                        time.sleep(0.05)
                    self.assertTrue(result.is_file(), "Launcher did not start Electron")
                    cwd, runtime_dir, app_path, pid, visible = result.read_text(encoding="utf-8-sig").splitlines()
                    # 探针写完结果后仍可能正在退出；等待其释放目录，再模拟移动。
                    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
                    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
                    kernel.OpenProcess.restype = wintypes.HANDLE
                    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
                    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
                    process = kernel.OpenProcess(0x00100000, False, int(pid))
                    if process:
                        try:
                            self.assertEqual(kernel.WaitForSingleObject(process, 10000), 0)
                        finally:
                            kernel.CloseHandle(process)
                    self.assertEqual(Path(cwd), root)
                    self.assertEqual(Path(runtime_dir), root / "node_modules/electron/dist")
                    self.assertEqual(app_path, ".")
                    self.assertEqual(visible, "True", "Launcher hid the native window")


class ModelPathMigrationTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name) / "project with spaces"
        self.config = self.root / "subtitle_app" / "config.json"
        root_patch = patch.object(electron_backend, "ROOT", self.root)
        config_patch = patch.object(electron_backend, "CONFIG", self.config)
        root_patch.start()
        config_patch.start()
        self.addCleanup(root_patch.stop)
        self.addCleanup(config_patch.stop)
        self.backend = electron_backend.DesktopBackend(Mock())

    def save_settings(self, **values):
        self.backend.save_settings({"glossary_texts": {}, **values}, permanent=True)
        return json.loads(self.config.read_text(encoding="utf-8"))

    def test_relative_model_uses_project_root_from_other_working_directory(self):
        self.assertNotEqual(Path.cwd(), self.root)
        actual = self.backend.resolve_model_dir("models/whisper")
        self.assertEqual(actual, str(self.root / "models" / "whisper"))

    def test_saved_internal_model_follows_moved_project(self):
        model = self.root / "models" / "whisper"
        saved = self.save_settings(model_dir=str(model))["whisper"]["model_dir"]
        self.assertEqual(saved, "models/whisper")
        moved = self.root.with_name("moved project")
        with patch.object(electron_backend, "ROOT", moved):
            self.assertEqual(self.backend.resolve_model_dir(saved), str(moved / "models" / "whisper"))

    def test_external_model_is_preserved(self):
        external = self.root.parent / "shared models" / "whisper"
        saved = self.save_settings(model_dir=str(external))["whisper"]["model_dir"]
        self.assertEqual(saved, str(external))

    def test_empty_model_stays_empty(self):
        self.assertEqual(self.backend.resolve_model_dir(""), "")
        self.assertEqual(self.save_settings(model_dir="")["whisper"]["model_dir"], "")

    def test_permanent_settings_preserve_unrelated_fields_and_batch_size(self):
        self.config.parent.mkdir(parents=True)
        original = {"translation": {"api_timeout": 60, "batch_size": 40}}
        self.config.write_text(json.dumps(original), encoding="utf-8")
        self.backend.settings["translation_batch_size"] = 40
        saved = self.save_settings(
            translation_only=True, send_all=True, glossary_texts={"zh": "ぬね=甲名"},
        )["translation"]
        self.assertEqual(saved["batch_size"], 40)
        self.assertEqual(saved["api_timeout"], 60)
        self.assertTrue(saved["translation_only"])
        self.assertTrue(saved["send_all"])
        self.assertEqual(saved["name_glossaries"], {"zh": {"ぬね": "甲名"}})
        self.assertEqual(self.save_settings(translation_batch_size=50)["translation"]["batch_size"], 50)
