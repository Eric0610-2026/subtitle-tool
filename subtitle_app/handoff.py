#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本机应用间媒体导入协议。"""
import json
from typing import List, Optional, Tuple


# 仅监听 127.0.0.1；下载器以同一端口发送一行 JSON 请求。
HANDOFF_PORT = 49732
MAX_HANDOFF_MESSAGE_BYTES = 64 * 1024
MAX_HANDOFF_PATHS = 200


def decode_handoff_request(raw: bytes) -> Tuple[Optional[List[str]], Optional[str]]:
    """解析下载器发来的单行 JSON，返回 ``(paths, error)``。"""
    if not raw or len(raw) > MAX_HANDOFF_MESSAGE_BYTES:
        return None, "请求为空或过大"
    try:
        request = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, "请求格式无效"
    if not isinstance(request, dict) or request.get("action") != "add_media":
        return None, "不支持的请求"
    paths = request.get("paths")
    if not isinstance(paths, list) or not paths or len(paths) > MAX_HANDOFF_PATHS:
        return None, "文件列表无效"

    cleaned = []
    for path in paths:
        if not isinstance(path, str) or not path.strip() or len(path) > 4096:
            return None, "文件路径无效"
        cleaned.append(path.strip())
    return cleaned, None


def encode_handoff_response(ok: bool, **extra) -> bytes:
    """将处理结果编码为单行 JSON 响应。"""
    response = {"ok": ok, **extra}
    return (json.dumps(response, ensure_ascii=False) + "\n").encode("utf-8")
