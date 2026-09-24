#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""托盘隐藏、恢复和退出的窗口交互。"""
import os
import unittest
import uuid
from contextlib import ExitStack
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QMessageBox, QSystemTrayIcon

from subtitle_app.qt_app import SubtitleApp, _claim_single_instance


class TestTrayWindow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.patches = ExitStack()
        for method in (
            "_start_handoff_server", "_update_model_status", "_restore_window_state",
            "_save_window_state", "_run_startup_checks", "_setup_tray",
        ):
            self.patches.enter_context(patch.object(SubtitleApp, method))
        self.window = SubtitleApp()
        self.window._tray_icon = Mock()
        self.window.show()

    def tearDown(self):
        self.window._exiting = True
        with patch.object(self.window.worker, "stop"), patch("subtitle_app.local_service.shutdown_owned"):
            self.window.close()
        self.patches.close()

    def test_close_hides_and_tray_click_restores(self):
        with patch.object(self.window.worker, "stop") as stop:
            self.window.close()
        self.assertFalse(self.window.isVisible())
        self.assertFalse(self.window._closing)
        stop.assert_not_called()

        self.window._on_tray_activated(QSystemTrayIcon.Trigger)
        self.assertTrue(self.window.isVisible())

    def test_second_launch_restores_existing_window(self):
        name = "subtitle-tool-test-" + uuid.uuid4().hex
        lock, server = _claim_single_instance(self.window._show_window, name)
        try:
            self.window.close()
            self.assertFalse(self.window.isVisible())
            self.assertIsNone(_claim_single_instance(Mock(), name))
            self.app.processEvents()
            self.assertTrue(self.window.isVisible())
        finally:
            server.close()
            lock.unlock()

    def test_hidden_embed_confirmation_waits_for_restore(self):
        self.window.close()
        event = {"response": Mock(), "file_name": "example.mp4"}
        with patch("subtitle_app.qt_app.system_notify") as notify, patch(
            "subtitle_app.qt_app.show_embed_confirm_dialog"
        ) as dialog:
            self.window._handle_pause_before_embed(event)
            notify.assert_called_once()
            dialog.assert_not_called()
            self.window._on_tray_activated(QSystemTrayIcon.Trigger)
            self.app.processEvents()
            dialog.assert_called_once_with(self.window, event)

    def test_running_task_requires_confirmation_to_exit(self):
        with patch.object(self.window, "_has_active_tasks", return_value=True), patch.object(
            QMessageBox, "question", side_effect=[QMessageBox.No, QMessageBox.Yes]
        ), patch.object(self.window.worker, "stop") as stop, patch(
            "subtitle_app.local_service.shutdown_owned"
        ):
            self.window._quit_from_tray()
            self.assertFalse(self.window._closing)
            stop.assert_not_called()
            self.window._quit_from_tray()
            self.assertTrue(self.window._closing)
            stop.assert_called_once()


if __name__ == "__main__":
    unittest.main()
