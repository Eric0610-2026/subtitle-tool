"""本地模型管理：模拟推理与服务，验证操作、状态、互斥和真实 Qt 渲染。"""
import os
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import QApplication, QAbstractButton, QLabel
from subtitle_app import qt_app, local_service
from subtitle_app.dialogs import ModelManagerDialog


class ModelManagerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setStyle("Fusion")
        if not QFontDatabase.families():
            # Windows offscreen 插件不会自动枚举系统字体；显式载入用于实际文字渲染。
            fonts = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
            for name in ("msyh.ttc", "msyhbd.ttc", "segoeui.ttf"):
                if (fonts / name).is_file():
                    QFontDatabase.addApplicationFont(str(fonts / name))
        font = QFont()
        font.setFamilies(["Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI"])
        cls.app.setFont(font)

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch.object(qt_app, "APP_DIR", self.root))
        for method in ("_start_handoff_server", "_restore_window_state", "_save_window_state",
                       "_run_startup_checks", "_setup_tray"):
            self.stack.enter_context(patch.object(qt_app.SubtitleApp, method))
        self.stack.enter_context(patch.object(local_service, "is_service_running", return_value=False))
        self.stack.enter_context(patch.object(local_service, "is_owned_service_running", return_value=False))
        self.stack.enter_context(patch.object(local_service, "is_service_listening", return_value=False))
        self.stack.enter_context(patch.object(local_service, "shutdown_owned", return_value=True))
        self.stack.enter_context(patch.object(local_service, "shutdown_service", return_value=(True, "Hy-MT2 已卸下")))
        self.window = qt_app.SubtitleApp()
        self.window._tray_icon = None
        self.dialog = ModelManagerDialog(self.window)
        self.window._model_manager = self.dialog
        self.addCleanup(self.close_window)
        self.post = self.stack.enter_context(patch.object(self.window.signal_bridge, "post"))

    def close_window(self):
        self.dialog.close()
        self.window._exiting = True
        with patch.object(self.window.worker, "stop"), patch.object(self.window.worker.transcriber, "release_model"):
            self.window.close()

    def result(self):
        return [call.args[0] for call in self.post.call_args_list
                if call.args[0]["type"] == "local_model_loaded"][-1]

    def test_whisper_load_uses_configured_model_and_shared_transcriber(self):
        model_dir = self.root / "local model"
        model_dir.mkdir()
        (model_dir / "model.bin").write_bytes(b"fake")
        opts = {"model_dir": str(model_dir), "device": "cpu", "compute_type": "int8"}
        with patch.object(self.window.worker.transcriber, "load_whisper_model") as load:
            self.window._load_local_model_worker("whisper", "load", opts)
        load.assert_called_once_with(model_dir, "cpu", "int8", self.post)
        self.assertTrue(self.result()["ok"])

    def test_missing_whisper_is_local_only_and_reports_failure(self):
        with patch.object(self.window.worker.transcriber, "load_whisper_model") as load:
            self.window._load_local_model_worker("whisper", "load", {"model_dir": str(self.root)})
        load.assert_not_called()
        self.assertFalse(self.result()["ok"])
        self.assertIn("model.bin", self.result()["detail"])

    def test_whisper_unload_releases_shared_cache(self):
        with patch.object(self.window.worker.transcriber, "release_model") as release:
            self.window._load_local_model_worker("whisper", "unload")
        release.assert_called_once()
        self.assertTrue(self.result()["ok"])

    def test_hy_load_uses_existing_service_and_progress_bridge(self):
        def ensure(**kwargs):
            self.assertFalse(kwargs["stop_check"]())
            kwargs["on_progress"](15)
            return True, "ready", True
        with patch.object(local_service, "ensure_running", side_effect=ensure):
            self.window._load_local_model_worker()
        self.assertTrue(self.result()["ok"])
        self.assertTrue(any(c.args[0]["type"] == "local_model_progress" for c in self.post.call_args_list))

    def test_hy_unload_uses_unified_service_cleanup(self):
        with patch.object(local_service, "shutdown_service", return_value=(True, "Hy-MT2 已卸下")) as stop:
            self.window._load_local_model_worker("hy-mt2", "unload")
        stop.assert_called_once()
        self.assertTrue(self.result()["ok"])

    def test_external_hy_service_can_be_unloaded(self):
        with patch.object(local_service, "is_service_running", return_value=True), \
                patch.object(local_service, "shutdown_service", return_value=(True, "Hy-MT2 已卸下")) as stop:
            self.window._load_local_model_worker("hy-mt2", "unload")
        stop.assert_called_once()
        self.assertTrue(self.result()["ok"])
        self.assertIn("已卸下", self.result()["detail"])

    def test_failed_hy_shutdown_is_not_reported_as_success(self):
        with patch.object(local_service, "shutdown_service", return_value=(False, "服务进程仍在运行")):
            self.window._load_local_model_worker("hy-mt2", "unload")
        self.assertFalse(self.result()["ok"])
        self.assertIn("仍在运行", self.result()["detail"])

    def test_unready_external_service_can_be_unloaded_and_retried_after_failure(self):
        with patch.object(local_service, "is_service_listening", return_value=True), \
                patch("threading.Thread") as thread:
            self.window._refresh_model_manager()
            thread.call_args.kwargs["target"]()
        self.window._on_local_model_status(self.post.call_args.args[0])
        self.assertIn("服务未就绪", self.dialog.status_labels["hy-mt2"].text())
        self.assertTrue(self.dialog.buttons["hy-mt2", "unload"].isEnabled())
        self.assertFalse(self.dialog.buttons["hy-mt2", "load"].isEnabled())
        with patch.object(local_service, "shutdown_service", return_value=(False, "权限不足")):
            self.window._load_local_model_worker("hy-mt2", "unload")
        with patch.object(local_service, "is_service_listening", return_value=True), \
                patch("threading.Thread") as thread, patch.object(qt_app, "system_notify"):
            self.window._on_local_model_loaded(self.result())
            thread.call_args.kwargs["target"]()
        self.window._on_local_model_status(self.post.call_args.args[0])
        self.assertTrue(self.dialog.buttons["hy-mt2", "unload"].isEnabled())
        self.assertIn("权限不足", self.dialog.result_label.text())

    def test_service_exception_still_completes_and_resets_controls(self):
        self.window._loading_local_model = True
        self.window._local_model_operation = ("hy-mt2", "load")
        self.window.start_btn.setEnabled(False)
        with patch.object(local_service, "ensure_running", side_effect=RuntimeError("startup failed")):
            self.window._load_local_model_worker()
        self.assertFalse(self.result()["ok"])
        with patch.object(self.window, "_refresh_model_manager"), patch.object(qt_app, "system_notify"):
            self.window._on_local_model_loaded(self.result())
        self.assertFalse(self.window._loading_local_model)
        self.assertTrue(self.window.start_btn.isEnabled())
        self.assertIn("startup failed", self.dialog.result_label.text())

    def test_task_running_blocks_model_operation(self):
        self.window.worker.thread = Mock()
        self.window.worker.thread.is_alive.return_value = True
        with patch("threading.Thread") as thread:
            self.window._operate_local_model("whisper", "unload")
        thread.assert_not_called()
        self.assertFalse(self.window._loading_local_model)
        self.assertTrue(all(not b.isEnabled() for b in self.dialog.buttons.values()))

    def test_model_operation_blocks_start_and_retry_even_after_dialog_closed(self):
        self.window._loading_local_model = True
        self.window.video_jobs = [self.root / "movie.mp4"]
        with patch.object(qt_app.QMessageBox, "warning") as warning, \
                patch.object(self.window.worker, "start") as start:
            self.window._start()
            self.window._retry()
        self.assertEqual(warning.call_count, 2)
        start.assert_not_called()

    def test_operation_captures_config_and_prevents_duplicate_threads(self):
        self.window.settings_data["model_dir"] = "models/whisper"
        with patch("threading.Thread") as thread:
            self.window._operate_local_model("whisper", "load")
            self.window._operate_local_model("hy-mt2", "load")
        self.assertEqual(thread.call_count, 1)
        opts = thread.call_args.kwargs["args"][2]
        self.assertEqual(opts["model_dir"], str(self.root / "models" / "whisper"))
        self.assertEqual(opts["device"], qt_app.cfg.whisper.device)
        self.assertFalse(self.window.start_btn.isEnabled())

    def test_stale_status_cannot_override_current_operation(self):
        self.window._model_status_generation = 2
        self.window._on_local_model_status({"generation": 1, "states": {"whisper": {"loaded": True}}})
        self.assertEqual(self.window._local_model_states, {})

    def test_thread_start_failure_restores_controls(self):
        with patch("threading.Thread") as thread, \
                patch.object(self.window, "_refresh_model_manager"), patch.object(qt_app, "system_notify"):
            thread.return_value.start.side_effect = RuntimeError("thread failed")
            self.window._operate_local_model("hy-mt2", "load")
        self.assertFalse(self.window._loading_local_model)
        self.assertTrue(self.window.start_btn.isEnabled())
        self.assertTrue(self.window.retry_btn.isEnabled())
        self.assertIn("thread failed", self.dialog.result_label.text())

    def test_status_probe_runs_in_background(self):
        with patch("threading.Thread") as thread:
            self.window._refresh_model_manager()
            self.window._refresh_model_manager()
        self.assertEqual(thread.call_count, 1)
        self.assertTrue(thread.call_args.kwargs["daemon"])
        thread.call_args.kwargs["target"]()
        event = self.post.call_args.args[0]
        self.window._on_local_model_status(event)
        self.assertFalse(self.window._model_status_checking)
        self.assertTrue(self.dialog.buttons["whisper", "load"].isEnabled())

    def test_status_uses_loaded_model_after_settings_directory_changes(self):
        transcriber = self.window.worker.transcriber
        model_dir = self.root / "faster-whisper-large-v3-turbo"
        transcriber._model_cache[f"{model_dir}|cpu|int8"] = ("cpu", "int8", object())
        self.addCleanup(transcriber._model_cache.clear)
        self.window.settings_data["model_dir"] = str(self.root / "different-model")
        with patch("threading.Thread") as thread:
            self.window._refresh_model_manager()
        thread.call_args.kwargs["target"]()
        self.window._on_local_model_status(self.post.call_args.args[0])
        text = self.dialog.status_labels["whisper"].text()
        self.assertIn("已加载", text)
        self.assertIn(model_dir.name, text)
        self.assertNotIn("different-model", text)
        self.assertFalse(self.dialog.buttons["whisper", "load"].isEnabled())
        self.assertTrue(self.dialog.buttons["whisper", "unload"].isEnabled())
        transcriber._model_cache.clear()
        with patch("threading.Thread") as thread:
            self.window._refresh_model_manager()
        thread.call_args.kwargs["target"]()
        self.window._on_local_model_status(self.post.call_args.args[0])
        self.assertEqual(self.dialog.status_labels["whisper"].text(), "未加载")

    def test_home_has_manager_entry_without_loaded_status_label(self):
        self.assertIn("本地模型管理", self.window.load_model_btn.text())
        self.assertFalse(any("当前加载" in label.text() or "当前模型" in label.text()
                             for label in self.window.findChildren(QLabel)))
        with patch.object(local_service, "is_service_running") as probe:
            self.window._handle_event({"type": "model_loaded"})
        probe.assert_not_called()

    def test_model_loaded_event_refreshes_visible_manager(self):
        self.dialog.show()
        with patch.object(self.window, "_refresh_model_manager") as refresh:
            self.window._handle_event({"type": "model_loaded"})
        refresh.assert_called_once()

    def test_exit_during_whisper_load_releases_late_model(self):
        model_dir = self.root / "whisper"
        model_dir.mkdir()
        (model_dir / "model.bin").write_bytes(b"fake")
        def finish_load(*args):
            self.window._closing = True
        with patch.object(self.window.worker.transcriber, "load_whisper_model", side_effect=finish_load), \
                patch.object(self.window.worker.transcriber, "release_model") as release:
            self.window._load_local_model_worker("whisper", "load", {
                "model_dir": str(model_dir), "device": "cpu", "compute_type": "int8"})
        release.assert_called_once()

    def test_dialog_render_and_controls_in_both_themes(self):
        states = {"whisper": {"loaded": False}, "hy-mt2": {"loaded": True, "owned": False}}
        self.window.show()
        self.dialog.show()
        actions = []
        self.dialog.operation_requested.connect(lambda *args: actions.append(args))
        for dark in (False, True):
            self.window.dark_mode = dark
            self.window.colors = qt_app.DARK if dark else qt_app.LIGHT
            self.window._apply_style()
            self.dialog.update_state(states)
            self.app.processEvents()
            self.assertFalse(self.dialog.grab().isNull())
            self.assertTrue(self.dialog.buttons["whisper", "load"].isEnabled())
            self.assertFalse(self.dialog.buttons["whisper", "unload"].isEnabled())
            self.assertTrue(self.dialog.buttons["hy-mt2", "unload"].isEnabled())
            self.assertIn("外部启动", self.dialog.status_labels["hy-mt2"].text())
            for button in self.dialog.findChildren(QAbstractButton):
                self.assertTrue(self.dialog.rect().contains(button.mapTo(self.dialog, button.rect().bottomRight())))
                self.assertGreaterEqual(button.width(), button.fontMetrics().horizontalAdvance(button.text()))
                self.assertEqual(button.cursor().shape(), Qt.PointingHandCursor if button.isEnabled() else Qt.ArrowCursor)
            self.dialog.buttons["whisper", "load"].click()
            self.assertEqual(actions[-1], ("whisper", "load"))
            output = os.environ.get("SUBTITLE_MODEL_PREVIEW_DIR")
            if output:
                folder = Path(output)
                folder.mkdir(parents=True, exist_ok=True)
                mode = "dark" if dark else "light"
                self.window.grab().save(str(folder / f"main-{mode}.png"))
                self.dialog.grab().save(str(folder / f"model-manager-unloaded-{mode}.png"))
                self.dialog.update_state({
                    "whisper": {"loaded": True, "name": "faster-whisper-large-v3-turbo"},
                    "hy-mt2": {"loaded": True, "owned": False},
                })
                self.app.processEvents()
                self.dialog.grab().save(str(folder / f"model-manager-{mode}.png"))
        self.dialog.update_state(states, busy=("whisper", "load"))
        self.assertIn("加载中", self.dialog.status_labels["whisper"].text())
        self.assertTrue(all(not b.isEnabled() for b in self.dialog.buttons.values()))
        self.dialog.update_state({"hy-mt2": {"loaded": False, "owned": True}})
        self.assertTrue(self.dialog.buttons["hy-mt2", "unload"].isEnabled())


class ServiceCancellationTest(unittest.TestCase):
    def test_cancelled_load_does_not_launch_or_probe(self):
        with patch.object(local_service, "_probe") as probe, patch.object(local_service, "_launch_owned") as launch:
            ok, detail, first = local_service.ensure_running(stop_check=lambda: True)
        self.assertFalse(ok)
        self.assertIn("取消", detail)
        launch.assert_not_called()
        probe.assert_not_called()

    def test_cancel_after_launch_cleans_owned_process(self):
        proc = Mock(pid=12345)
        proc.poll.return_value = None
        state = iter((False, False, True))
        with patch.object(local_service, "_owned_proc", None), \
                patch.object(local_service, "_started_by_us", False), \
                patch.object(local_service, "_ready_announced", False), \
                patch.object(local_service, "_probe", return_value=False), \
                patch.object(local_service, "_port_listening", return_value=False), \
                patch.object(local_service, "_SERVER") as server, \
                patch.object(local_service, "_find_model", return_value=Path("fake.gguf")), \
                patch.object(local_service, "_launch_owned", return_value=proc) as launch, \
                patch.object(local_service, "shutdown_owned", return_value=True) as stop:
            server.exists.return_value = True
            ok, detail, _ = local_service.ensure_running(stop_check=lambda: next(state))
        self.assertFalse(ok)
        self.assertIn("取消", detail)
        launch.assert_called_once()
        stop.assert_called_once()


if __name__ == "__main__":
    unittest.main()
