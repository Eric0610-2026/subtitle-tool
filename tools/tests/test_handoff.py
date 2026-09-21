#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""下载器导入协议的无界面单元测试。"""
import json
import unittest

from subtitle_app.handoff import (
    MAX_HANDOFF_PATHS,
    decode_handoff_request,
    encode_handoff_response,
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

    def test_response_is_single_line_json(self):
        response = encode_handoff_response(True, added=2, skipped=1)

        self.assertTrue(response.endswith(b"\n"))
        self.assertEqual(
            json.loads(response.decode("utf-8")),
            {"ok": True, "added": 2, "skipped": 1},
        )


if __name__ == "__main__":
    unittest.main()
