#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""下载器导入协议的无界面单元测试。"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from subtitle_app import electron_backend
from subtitle_app.handoff import (
    MAX_HANDOFF_PATHS,
    decode_handoff_request,
)


class TestHandoffProtocol(unittest.TestCase):
    def test_accepts_valid_media_request(self):
        request = json.dumps({
            "action": "add_media",
            "paths": [r"D:\video one.mp4", r"D:\video two.mkv"],
        }).encode("utf-8")

        paths, error = decode_handoff_request(request)

        self.assertIsNone(error)
        self.assertEqual(paths, [r"D:\video one.mp4", r"D:\video two.mkv"])

    def test_rejects_invalid_requests(self):
        cases = [
            b"not json",
            json.dumps({"action": "unknown", "paths": ["a.mp4"]}).encode(),
            json.dumps({"action": "add_media", "paths": []}).encode(),
            json.dumps({"action": "add_media", "paths": [" "]}).encode(),
        ]
        for request in cases:
            with self.subTest(request=request):
                paths, error = decode_handoff_request(request)
                self.assertIsNone(paths)
                self.assertIsNotNone(error)

    def test_rejects_too_many_paths(self):
        request = json.dumps({
            "action": "add_media",
            "paths": [f"D:\\{i}.mp4" for i in range(MAX_HANDOFF_PATHS + 1)],
        }).encode("utf-8")

        paths, error = decode_handoff_request(request)

        self.assertIsNone(paths)
        self.assertEqual(error, "文件列表无效")

    def test_backend_handoff_reports_added_skipped_and_invalid(self):
        with tempfile.TemporaryDirectory() as d, patch.object(electron_backend, "ROOT", Path(d)):
            file = Path(d) / "样片.mp4"
            file.touch()
            backend = electron_backend.DesktopBackend(Mock())
            request = json.dumps({"action": "add_media", "paths": [str(file), str(file), "invalid.mp4"]})
            self.assertEqual(backend.handoff(request), {"ok": True, "added": 1, "skipped": 1, "invalid": 1})
            self.assertEqual(backend.jobs, [file.resolve()])


if __name__ == "__main__":
    unittest.main()
