#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
系统通知模块：winotify 优先，PowerShell 备选。

从 qt_app.py 抽取，保持行为完全一致。标题/正文在拼接进 PowerShell
脚本前经过字符白名单过滤，防止命令注入。
"""
import base64
import logging
import re
import subprocess

from .config import cfg

logger = logging.getLogger(__name__)

# PowerShell 脚本内允许的字符（标题/正文白名单）
_TITLE_ALLOWED = r"[^\w\s\-_.()\[\]【】]"
_MSG_ALLOWED = r"[^\w\s\-_.()\[\]【】，。！？、：；]"


def notify(title: str, msg: str) -> None:
    """安全的系统通知，避免命令注入"""
    safe_title = re.sub(_TITLE_ALLOWED, "", title)[:64]
    safe_msg = re.sub(_MSG_ALLOWED, "", msg)[:200]

    try:
        from winotify import Notification, audio
        toast = Notification(
            app_id="字幕工具",
            title=safe_title,
            msg=safe_msg,
            duration="short"
        )
        toast.set_audio(audio.Default, loop=False)
        toast.show()
        return
    except ImportError:
        pass
    except Exception as e:
        logger.debug("winotify 通知失败: %s，尝试 PowerShell 备选方案", e)

    try:
        ps_script = (
            "Add-Type -AssemblyName System.Windows.Forms;"
            "$n=New-Object System.Windows.Forms.NotifyIcon;"
            "$n.Icon=[System.Drawing.SystemIcons]::Information;"
            '$n.BalloonTipIcon="Info";'
            f'$n.BalloonTipTitle="{safe_title}";'
            f'$n.BalloonTipText="{safe_msg}";'
            "$n.Visible=$true;"
            f"$n.ShowBalloonTip({cfg.app.notification_duration_ms});"
            f"Start-Sleep -Seconds {cfg.app.notification_sleep_s};"
            "$n.Dispose()"
        )
        encoded = base64.b64encode(ps_script.encode("utf-16-le")).decode("ascii")
        subprocess.Popen(
            ["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW
        )
    except Exception as e:
        logger.debug("通知失败: %s", e)
