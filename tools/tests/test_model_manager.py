"""本地模型管理与服务取消的回归测试。"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from subtitle_app import electron_backend, local_service


class ModelManagerTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        root_patch = patch.object(electron_backend, "ROOT", self.root)
        root_patch.start()
        self.addCleanup(root_patch.stop)
        self.events = []
        self.backend = electron_backend.DesktopBackend(lambda message: self.events.append(message["event"]))

    def test_whisper_load_uses_configured_path_and_shared_transcriber(self):
        model_dir = self.root / "whisper"
        model_dir.mkdir()
        (model_dir / "model.bin").touch()
        settings = {**self.backend.settings, "model_dir": str(model_dir)}
        with patch.object(self.backend.worker.transcriber, "load_whisper_model") as load, \
                patch.object(self.backend, "model_states", return_value={}):
            self.backend.change_model("whisper", "load", settings)
        load.assert_called_once_with(
            model_dir, electron_backend.cfg.whisper.device,
            electron_backend.cfg.whisper.compute_type, self.backend.emit,
        )

    def test_missing_whisper_does_not_load_a_remote_model(self):
        settings = {**self.backend.settings, "model_dir": str(self.root / "missing")}
        with patch.object(self.backend.worker.transcriber, "load_whisper_model") as load, \
                self.assertRaisesRegex(RuntimeError, "model.bin"):
            self.backend.change_model("whisper", "load", settings)
        load.assert_not_called()

    def test_whisper_unload_releases_shared_transcriber(self):
        with patch.object(self.backend.worker.transcriber, "release_model") as release, \
                patch.object(self.backend, "model_states", return_value={}):
            self.backend.change_model("whisper", "unload", {})
        release.assert_called_once()

    def test_hy_load_reports_progress_and_observes_stop(self):
        def ensure_running(stop_check, on_progress):
            self.assertFalse(stop_check())
            on_progress(3)
            return True, "已就绪", False

        with patch.object(local_service, "ensure_running", side_effect=ensure_running) as ensure, \
                patch.object(self.backend, "model_states", return_value={}):
            self.backend.change_model("hy-mt2", "load", {})
        ensure.assert_called_once()
        self.assertTrue(any(event["type"] == "model_progress" for event in self.events))

    def test_hy_unload_uses_unified_service_cleanup_and_reports_failure(self):
        with patch.object(local_service, "shutdown_service", return_value=(False, "仍在运行")) as shutdown, \
                patch.object(self.backend, "model_states", return_value={}):
            self.backend.change_model("hy-mt2", "unload", {})
        shutdown.assert_called_once()
        log = self.events[0]
        self.assertEqual((log["level"], log["message"]), ("ERROR", "仍在运行"))

    def test_active_task_blocks_model_operations(self):
        self.backend.task_kind = "pipeline"
        with self.assertRaisesRegex(RuntimeError, "当前任务"):
            self.backend.operate_model("whisper", "load")

    def test_model_status_uses_resident_name_and_external_listener(self):
        with patch.object(self.backend.worker.transcriber, "loaded_model_name", return_value="resident"), \
                patch.object(local_service, "is_service_running", return_value=False), \
                patch.object(local_service, "is_owned_service_running", return_value=False), \
                patch.object(local_service, "is_service_listening", return_value=True):
            states = self.backend.model_states()
        self.assertEqual(states["whisper"], {"loaded": True, "name": "resident"})
        self.assertEqual(states["hy-mt2"], {"loaded": False, "owned": False, "running": True})


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
