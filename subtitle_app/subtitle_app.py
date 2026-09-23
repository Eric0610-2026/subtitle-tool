#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
入口：首次运行时检测依赖并自动安装，之后正常启动 Qt 界面。
"""
import subprocess
import sys
import hashlib
from pathlib import Path

_APP_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_APP_DIR))
_MARKER = _APP_DIR / "cache" / ".deps_installed"


def _msgbox(title, text):
    """ctypes 弹窗，不依赖 PySide6"""
    import ctypes
    ctypes.windll.user32.MessageBoxW(0, text, title, 0x10)


def _ensure_deps() -> bool:
    req = _APP_DIR / "tools" / "requirements.txt"
    if not req.exists():
        _msgbox("错误", f"未找到 {req}")
        return False
    # 绑定依赖清单及解释器路径/版本。旧版空标记或环境变化后重新安装。
    fingerprint = hashlib.sha256(
        req.read_bytes() + b"\0" + str(Path(sys.executable).resolve()).encode("utf-8")
        + b"\0" + sys.version.encode("utf-8")
    ).hexdigest()
    if _MARKER.exists() and _MARKER.read_text(encoding="utf-8").strip() == fingerprint:
        return True
    python = sys.executable.replace("pythonw.exe", "python.exe")
    try:
        proc = subprocess.run(
            [python, "-m", "pip", "install", "-r", str(req), "-q"],
            capture_output=True, text=True, timeout=300,
        )
    except subprocess.TimeoutExpired:
        _msgbox("依赖安装超时", "pip install 超过 5 分钟未完成，请检查网络连接")
        return False
    if proc.returncode != 0:
        _msgbox("依赖安装失败", proc.stderr[:500])
        return False
    _MARKER.parent.mkdir(parents=True, exist_ok=True)
    _MARKER.write_text(fingerprint, encoding="utf-8")
    return True


if __name__ == "__main__":
    if _ensure_deps():
        from subtitle_app.qt_app import main
        main()
    else:
        sys.exit(1)
