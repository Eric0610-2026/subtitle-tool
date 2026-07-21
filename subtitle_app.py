#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
入口：首次运行时检测依赖并自动安装，之后正常启动 Qt 界面。
"""
import subprocess
import sys
from pathlib import Path

_APP_DIR = Path(__file__).resolve().parent
_MARKER = _APP_DIR / ".deps_installed"


def _msgbox(title, text):
    """ctypes 弹窗，不依赖 PySide6"""
    import ctypes
    ctypes.windll.user32.MessageBoxW(0, text, title, 0x10)


def _ensure_deps() -> bool:
    if _MARKER.exists():
        return True
    req = _APP_DIR / "requirements.txt"
    if not req.exists():
        _msgbox("错误", f"未找到 {req}")
        return False
    python = sys.executable.replace("pythonw.exe", "python.exe")
    proc = subprocess.run(
        [python, "-m", "pip", "install", "-r", str(req), "-q"],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        _msgbox("依赖安装失败", proc.stderr[:500])
        return False
    _MARKER.touch()
    return True


if __name__ == "__main__":
    if _ensure_deps():
        from subtitle_app.qt_app import main
        main()
    else:
        sys.exit(1)
