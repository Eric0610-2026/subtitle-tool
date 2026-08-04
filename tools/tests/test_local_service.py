#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""测试 local_service：服务探测、自动拉起（含就绪/失败/超时回滚）"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from subtitle_app import local_service


class _FakeProc:
    """模拟 llama-server 子进程（保持运行，需显式 terminate 才会退出）"""
    def __init__(self):
        self.pid = 12345
        self.returncode = None

    def poll(self):
        return None  # 一直存活，除非被 terminate

    def terminate(self):
        self.returncode = 0

    def wait(self, timeout=None):
        self.returncode = 0


def _reset_state():
    local_service._owned_proc = None
    local_service._started_by_us = False
    local_service._ready_announced = False


class ShutdownRunningTest(unittest.TestCase):
    """shutdown_running：退出时按端口清理 127.0.0.1:8080 上的 llama-server（含残留）"""

    def setUp(self):
        _reset_state()

    @staticmethod
    def _netstat_mock(stdout_text):
        return patch("subtitle_app.local_service.subprocess.run",
                     return_value=unittest.mock.MagicMock(stdout=stdout_text))

    def test_no_listener_and_kills_listening(self):
        # 无 8080 监听 → 不动，不触发 netstat
        with self._netstat_mock("  TCP    127.0.0.1:9999    0.0.0.0:0    LISTENING   1\n") as mrun, \
             patch("subtitle_app.local_service._port_listening", return_value=False):
            ok = local_service.shutdown_running()
        self.assertFalse(ok)
        mrun.assert_not_called()  # 无监听时不应触发 netstat
        # 8080 被监听 → taskkill 杀掉该 pid
        netstat_out = (
            "  TCP    127.0.0.1:8080    0.0.0.0:0              LISTENING       34368\n"
            "  TCP    127.0.0.1:9000    0.0.0.0:0              LISTENING       7777\n"
        )
        with self._netstat_mock(netstat_out) as mrun, \
             patch("subtitle_app.local_service._port_listening", return_value=True):
            ok = local_service.shutdown_running()
        self.assertTrue(ok)
        taskkill_calls = [c for c in mrun.call_args_list
                          if c.args and c.args[0] and c.args[0][0] == "taskkill"]
        self.assertEqual(len(taskkill_calls), 1)
        self.assertIn("34368", taskkill_calls[0].args[0])

    def test_shutdown_owned_force_kills_when_terminate_ineffective(self):
        """terminate 后进程仍存活 → taskkill 兜底强杀，避免退出卡顿/残留"""

        class _StubbornProc(_FakeProc):
            def terminate(self):
                pass  # 假装 terminate 无效，poll 永远返回 None

        fake = _StubbornProc()
        local_service._owned_proc = fake
        with patch("subtitle_app.local_service.subprocess.run") as mrun:
            local_service.shutdown_owned()
        taskkill_calls = [c for c in mrun.call_args_list
                          if c.args and c.args[0] and c.args[0][0] == "taskkill"]
        self.assertEqual(len(taskkill_calls), 1)
        self.assertIn(str(fake.pid), taskkill_calls[0].args[0])
        self.assertIsNone(local_service._owned_proc)


class LocalServiceTest(unittest.TestCase):
    def setUp(self):
        _reset_state()

    def tearDown(self):
        _reset_state()

    def test_probe_and_listening_detection(self):
        # 未监听端口：连接失败/超时均视为不可用
        with patch.object(local_service, "_PORT", 59999), \
                patch.object(local_service, "_HOST", "127.0.0.1"):
            self.assertFalse(local_service._probe(0.3))
            self.assertFalse(local_service._port_listening(0.2))
        # 自占一个端口：_port_listening 应识别出已被监听
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        port = s.getsockname()[1]
        try:
            with patch.object(local_service, "_PORT", port), \
                    patch.object(local_service, "_HOST", "127.0.0.1"):
                self.assertTrue(local_service._port_listening(0.2))
        finally:
            s.close()

    def test_find_model_empty_and_first_gguf(self):
        # 空目录 → None
        with tempfile.TemporaryDirectory() as d, \
                patch.object(local_service, "_MODELS_DIR", Path(d)):
            self.assertIsNone(local_service._find_model())
        # 有 gguf → 返回第一个（文件名排序后）
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            (p / "Hy-MT2-1.8B-Q8_0.gguf").write_bytes(b"x")
            (p / "other.gguf").write_bytes(b"y")
            with patch.object(local_service, "_MODELS_DIR", p):
                self.assertEqual(local_service._find_model().name, "Hy-MT2-1.8B-Q8_0.gguf")

    def test_ensure_when_no_server_bin(self):
        with patch.object(local_service, "_SERVER", Path("Z:/__nonexistent__/llama-server.exe")), \
                patch.object(local_service, "_probe", lambda *a, **k: False), \
                patch.object(local_service, "_port_listening", lambda *a, **k: False):
            ok, detail, _ = local_service.ensure_running(0.5)
            self.assertFalse(ok)
            self.assertIn("找不到服务程序", detail)

    def test_ensure_launches_and_waits_ready(self):
        with tempfile.TemporaryDirectory() as d:
            mdir = Path(d)
            (mdir / "m.gguf").write_bytes(b"x")
            calls = []

            def fake_probe(*a, **k):
                calls.append(1)
                return len(calls) >= 3  # 前两次失败，模拟「启动需要时间」

            server = Path(d) / "llama-server.exe"
            server.write_bytes(b"")
            launches = []
            fake_proc = _FakeProc()

            with patch.object(local_service, "_SERVER", server), \
                    patch.object(local_service, "_MODELS_DIR", mdir), \
                    patch.object(local_service, "_probe", fake_probe), \
                    patch.object(local_service, "_port_listening", lambda *a, **k: False), \
                    patch.object(local_service, "_launch_owned",
                                 lambda m: launches.append(m) or fake_proc):
                ok, _, first = local_service.ensure_running(timeout=2)
                self.assertTrue(ok)
                self.assertTrue(first)
                self.assertEqual(len(launches), 1)
                # 服务已就绪：第二次调用不再拉起
                ok2, _, first2 = local_service.ensure_running(timeout=0.3)
                self.assertTrue(ok2)
                self.assertFalse(first2)
                self.assertEqual(len(launches), 1)

    def test_ensure_aborts_on_timeout(self):
        with tempfile.TemporaryDirectory() as d:
            server = Path(d) / "llama-server.exe"
            server.write_bytes(b"")
            mdir = Path(d)
            (mdir / "m.gguf").write_bytes(b"x")
            got_terminated = []

            class _T(_FakeProc):
                def terminate(self):
                    got_terminated.append(1)
                    super().terminate()

            with patch.object(local_service, "_SERVER", server), \
                    patch.object(local_service, "_MODELS_DIR", mdir), \
                    patch.object(local_service, "_probe", lambda *a, **k: False), \
                    patch.object(local_service, "_port_listening", lambda *a, **k: False), \
                    patch.object(local_service, "_launch_owned", lambda m: _T()):
                ok, detail, _ = local_service.ensure_running(timeout=0.2)
                self.assertFalse(ok)
                self.assertTrue(got_terminated)   # 超时后终止了进程
                self.assertIsNone(local_service._owned_proc)

    def test_shutdown_owned_terminates(self):
        got = []
        p = _FakeProc()
        p.terminate = lambda: got.append(1) or setattr(p, "returncode", 0)
        local_service._owned_proc = p
        local_service._started_by_us = True
        local_service.shutdown_owned()
        self.assertTrue(got)
        self.assertIsNone(local_service._owned_proc)


if __name__ == "__main__":
    unittest.main()