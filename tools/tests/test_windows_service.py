"""外部服务身份核实与关闭：终止操作全部模拟，原生检查仅读取测试进程。"""
import os
import socket
import sys
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

from subtitle_app import local_service, windows_service as service


@unittest.skipUnless(os.name == "nt", "Windows 服务管理")
class ExternalServiceTest(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.server = Path("F:/test project/tools/llama-server.exe")
        self.models = Path("F:/test project/models/hy-mt2")
        self.proc = Mock()
        self.proc.image_path.return_value = self.server
        self.proc.creation_ticks.return_value = 1234567890
        self.proc.terminate.return_value = True
        self.open = self.stack.enter_context(patch.object(service, "_ServiceProcess", return_value=self.proc))
        self.pids = self.stack.enter_context(patch.object(service, "_listening_pids", side_effect=[{42}, {42}, set()]))
        self.metadata = self.stack.enter_context(patch.object(service, "_process_metadata", return_value={
            "created": 1234567890, "command": "mocked command"}))
        self.arguments = self.stack.enter_context(patch.object(service, "_command_args", return_value=[
            str(self.server), "-m", str(self.models / "模型 中文.gguf")]))

    def stop(self):
        return service.stop_external_service("127.0.0.1", 8188, self.server, self.models)

    def test_verified_service_stops_and_closes_same_handle(self):
        ok, detail = self.stop()
        self.assertTrue(ok, detail)
        self.open.assert_called_once_with(42)
        self.proc.terminate.assert_called_once()
        self.proc.close.assert_called_once()

    def test_unrelated_executable_is_not_stopped(self):
        for path in (self.server.with_name("other.exe"), Path("F:/another/llama-server.exe")):
            with self.subTest(path=path):
                self.pids.side_effect = [{42}]
                self.proc.image_path.return_value = path
                self.assertFalse(self.stop()[0])
                self.proc.terminate.assert_not_called()

    def test_unverified_model_or_command_is_not_stopped(self):
        for arguments in (["server"], ["server", "-m", "relative/model.gguf"],
                          ["server", "-m", "F:/other/unrelated.gguf"],
                          ["server", "-m", "F:/other/not-hy-mt2.gguf"]):
            with self.subTest(arguments=arguments):
                self.pids.side_effect = [{42}]
                self.arguments.return_value = arguments
                self.assertFalse(self.stop()[0])
                self.proc.terminate.assert_not_called()

    def test_changed_creation_time_is_not_stopped(self):
        self.proc.creation_ticks.return_value += 100
        self.assertFalse(self.stop()[0])
        self.proc.terminate.assert_not_called()

    def test_changed_listener_is_not_stopped(self):
        self.pids.side_effect = [{42}, {43}]
        self.assertFalse(self.stop()[0])
        self.proc.terminate.assert_not_called()

    def test_replacement_after_stop_is_reported_and_not_followed(self):
        self.pids.side_effect = [{42}, {42}, {43}]
        ok, detail = self.stop()
        self.assertFalse(ok)
        self.assertIn("新的进程", detail)
        self.open.assert_called_once_with(42)
        self.proc.terminate.assert_called_once()

    def test_no_or_ambiguous_pid_never_opens_process(self):
        for pids in (set(), {42, 43}):
            with self.subTest(pids=pids):
                self.pids.side_effect = [pids]
                self.assertFalse(self.stop()[0])
                self.open.assert_not_called()

    def test_access_denied_or_query_timeout_is_reported(self):
        self.open.side_effect = PermissionError("access denied")
        self.assertIn("access denied", self.stop()[1])
        self.proc.terminate.assert_not_called()
        self.proc.close.assert_not_called()
        self.open.side_effect = None
        self.pids.side_effect = [{42}]
        self.metadata.side_effect = TimeoutError("query timeout")
        self.assertIn("query timeout", self.stop()[1])
        self.proc.terminate.assert_not_called()
        self.proc.close.assert_called_once()

    def test_termination_timeout_keeps_failure_state(self):
        self.proc.terminate.return_value = False
        self.assertFalse(self.stop()[0])
        self.proc.close.assert_called_once()


class UnifiedShutdownTest(unittest.TestCase):
    def test_owned_failure_does_not_touch_external_process(self):
        with patch.object(local_service, "shutdown_owned", return_value=False), \
                patch.object(local_service, "_port_listening") as listening, \
                patch.object(service, "stop_external_service") as stop:
            self.assertFalse(local_service.shutdown_service()[0])
        listening.assert_not_called()
        stop.assert_not_called()

    def test_no_listener_is_success_without_external_process_query(self):
        with patch.object(local_service, "shutdown_owned", return_value=True), \
                patch.object(local_service, "_port_listening", return_value=False), \
                patch.object(service, "stop_external_service") as stop:
            self.assertTrue(local_service.shutdown_service()[0])
        stop.assert_not_called()

    def test_external_service_result_and_identity_inputs_are_preserved(self):
        for result in ((True, "Hy-MT2 已卸下"), (False, "权限不足")):
            with self.subTest(result=result), \
                    patch.object(local_service, "shutdown_owned", return_value=True), \
                    patch.object(local_service, "_port_listening", return_value=True), \
                    patch.object(service, "stop_external_service", return_value=result) as stop:
                self.assertEqual(local_service.shutdown_service(), result)
                stop.assert_called_once_with(local_service._HOST, local_service._PORT,
                                             local_service._SERVER, local_service._MODELS_DIR)


@unittest.skipUnless(os.name == "nt", "Windows 原生只读检查")
class NativeReadOnlyTest(unittest.TestCase):
    def test_native_listener_table_identifies_test_socket(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            pids = service._listening_pids("127.0.0.1", listener.getsockname()[1])
            self.assertEqual(pids, {os.getpid()})

    @unittest.skipUnless(socket.has_ipv6, "IPv6 不可用")
    def test_native_dual_stack_listener_identifies_test_socket(self):
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as listener:
            listener.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
            listener.bind(("::", 0))
            listener.listen(1)
            port = listener.getsockname()[1]
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                self.assertEqual(service._listening_pids("127.0.0.1", port), {os.getpid()})

    def test_native_process_identity_and_cim_timestamp_agree(self):
        process = service._ServiceProcess(os.getpid())
        try:
            self.assertEqual(process.image_path().resolve(), Path(sys.executable).resolve())
            metadata = service._process_metadata(os.getpid())
            self.assertEqual(process.creation_ticks() // 10, int(metadata["created"]) // 10)
        finally:
            process.close()

    def test_native_argument_parser_handles_chinese_spaces_and_model_equals(self):
        model = local_service._MODELS_DIR / "中文 模型.gguf"
        arguments = service._command_args(f'"{local_service._SERVER}" --model="{model}"')
        self.assertTrue(service._is_hy_model(arguments, local_service._MODELS_DIR))
        self.assertEqual(arguments[1], f"--model={model}")
