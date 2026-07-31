#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
测试包公共初始化：统一测试进程的 stdout/stderr 编码为 UTF-8。

Windows 控制台默认 GBK 代码页下，unittest 打印中文断言信息会乱码
（如 �ݹ���ȳ���），导致失败用例难以定位。这里在 discover 导入测试
模块前强制重配置为标准流编码。
"""
import sys

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
