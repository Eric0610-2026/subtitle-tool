#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
本地 Hy-MT2 翻译服务管理：探测连通性 / 自动拉起 llama-server / 等待就绪 / 退出清理。

背景：本地翻译走本机 llama-server（OpenAI 兼容，http://127.0.0.1:8188）。
本模块让「在应用里选本地模式翻译」时能自动启动服务，而无需手动双击
start-local-model.bat。启动参数与 start-local-model.bat 保持一致。

关键点：
- 幂等：同一进程内只有第一个调用者负责拉起服务，其余并发调用等待就绪即返回。
- 生命周期：只在翻译阶段才拉起（ensure_running）；应用退出时 shutdown_owned 仅关闭本会话
  拉起的服务，避免影响用户手动启动的其他 llama-server。
- 失败可诊断：llama-server 的 stdout/stderr 落盘 cache/.llama-server.log，
  进程异常退出时读取日志尾部附在错误信息里（如端口被 Windows 预留导致 bind 失败）。
"""
import logging
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

# 不用 8080：该端口常被 Windows（Hyper-V/WSL）的动态端口预留占走，
# 导致 llama-server 无法绑定、启动即退出（见 _startup_diagnosis 的 netsh 提示）。
_PORT = 8188
_HOST = "127.0.0.1"          # 强制 IPv4 回环，翻译端点只可能是本机
_LOCAL_MODEL = "hy-mt2"      # llama-server 单模型名，仅用于缓存键与请求体
_READY_TIMEOUT = 120.0        # 模型加载最长等待（秒）
_POLL_INTERVAL = 0.4
_PROGRESS_INTERVAL = 15.0     # 等待期间向前端反馈进度的间隔（秒）

_lock = threading.Lock()
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SERVER = _PROJECT_ROOT / "tools" / "llama-cpp" / "llama-server.exe"
_MODELS_DIR = _PROJECT_ROOT / "models" / "hy-mt2"
_SERVER_LOG = _PROJECT_ROOT / "cache" / ".llama-server.log"

_owned_proc: Optional[subprocess.Popen] = None   # 本进程拉起的 llama-server
_started_by_us = False                           # 本进程曾发起启动
_ready_announced = False                         # 是否已向 UI 反馈过一次「就绪」

_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def service_url_prefix() -> str:
    """本地翻译服务 OpenAI 兼容 URL 前缀（供调用方识别"这是本地服务"）"""
    return f"http://{_HOST}:{_PORT}"


def translation_endpoint() -> str:
    """翻译请求的唯一目标地址：本机 llama-server 的 chat/completions。

    全应用只此一处定义翻译端点，调用方不得自行拼接或覆盖，
    以保证不存在任何指向外部服务器的代码路径。
    """
    return f"http://{_HOST}:{_PORT}/v1/chat/completions"


def local_model_name() -> str:
    """本地翻译模型名（llama-server 单模型加载，此值仅用于缓存键与请求体）"""
    return _LOCAL_MODEL


def _find_model() -> Optional[Path]:
    """返回第一个 .gguf 模型文件；未找到返回 None"""
    try:
        files = sorted(_MODELS_DIR.glob("*.gguf"))
    except OSError:
        return None
    return files[0] if files else None


def _probe(timeout: float = 0.8) -> bool:
    """探测 127.0.0.1 上本地翻译服务的 /health 是否就绪（与应用翻译 URL 一致）"""
    url = f"http://{_HOST}:{_PORT}/health"
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status == 200
    except Exception:
        return False


def _port_listening(timeout: float = 0.6) -> bool:
    """端口是否已被监听（不论是谁）——用于识别「端口被非翻译服务占用」"""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((_HOST, _PORT))
        return True
    except Exception:
        return False
    finally:
        s.close()


def _launch_owned(model: Path) -> subprocess.Popen:
    """以无窗口方式拉起 llama-server，参数与 start-local-model.bat 保持一致。
    显式 --host 127.0.0.1：确保监听 IPv4 回环（避免绑定到 ::1 导致应用连不上）。
    stdout/stderr 落盘 _SERVER_LOG：启动失败时可读取日志尾部给出真实原因
    （DEVNULL 会吞掉一切线索，只能看到笼统的退出码）。"""
    cmd = [
        str(_SERVER), "-m", str(model),
        "-c", "8192", "--port", str(_PORT), "--host", _HOST, "-ngl", "99",
        "--flash-attn", "on", "-t", "6",
        "--cache-type-k", "q8_0", "--cache-type-v", "q8_0",
        "--parallel", "1", "--jinja", "--n-predict", "-1",
    ]
    try:
        _SERVER_LOG.parent.mkdir(parents=True, exist_ok=True)
        fh = open(_SERVER_LOG, "w", encoding="utf-8", errors="replace")
    except OSError:
        fh = subprocess.DEVNULL  # 日志不可写时退回丢弃，不阻塞启动
    try:
        return subprocess.Popen(
            cmd,
            stdout=fh,
            stderr=subprocess.STDOUT,
            creationflags=_CREATE_NO_WINDOW,
        )
    finally:
        if fh is not subprocess.DEVNULL:
            fh.close()  # 子进程已继承句柄，父进程关闭不影响其继续写入


def _startup_diagnosis() -> str:
    """读取 llama-server 日志尾部，给出退出的真实原因与针对性修复提示。
    无日志/读不到时返回空串（调用方退回笼统提示）。"""
    try:
        lines = [ln.strip() for ln in
                 _SERVER_LOG.read_text(encoding="utf-8", errors="replace").splitlines()
                 if ln.strip()]
    except OSError:
        return ""
    tail = lines[-5:]
    if not tail:
        return ""
    diag = "\n服务日志（最后 %d 行）：\n%s" % (len(tail), "\n".join(tail))
    if any("couldn't bind" in ln for ln in tail):
        diag += (
            "\n\n端口 %d 无法绑定：netstat 查不到占用进程时，多半是被 Windows"
            "（Hyper-V/WSL）的动态端口预留占走了。请以管理员身份运行：\n"
            "  netsh int ipv4 add excludedportrange protocol=tcp startport=%d numberofports=1\n"
            "然后重试。"
        ) % (_PORT, _PORT)
    return diag


def ensure_running(timeout: float = _READY_TIMEOUT,
                   on_progress=None) -> Tuple[bool, str, bool]:
    """确保本地翻译服务可用，返回 (ok, 说明, 是否本进程首次拉起)。

    幂等：服务已就绪时立即返回；否则有且仅有一个调用者拉起进程，
    其余调用者与它一同等待就绪。等待期间若提供 on_progress(秒) 回调，
    会定期回调以便 UI 反馈进度（避免看起来像卡死）。
    调用不阻塞 UI（应在 worker 线程调用）。
    """
    global _owned_proc, _started_by_us, _ready_announced
    if _probe():
        return True, "本地服务已运行", False

    # 端口已监听但 /health 不通：非翻译服务占用了本服务端口，立即报错而不是干等
    if _port_listening():
        return False, (
            f"端口 {_PORT} 已被其他进程占用，且其中没有可用的翻译服务。"
            f"请用 `netstat -ano | findstr :{_PORT}` 找到占用进程并退出后重试。"
        ), False

    start_time = time.time()
    launcher = False
    crashed = False
    with _lock:
        # 自愈：服务就绪后进程又退出（崩溃/被杀）时，此前的拉起者早已返回，
        # 若不重置状态，后续调用会永远判"已启动过"而不重新拉起。
        if _started_by_us and _owned_proc is not None and _owned_proc.poll() is not None:
            logger.warning("llama-server 已退出（退出码 %s），重置状态以允许重新拉起",
                           _owned_proc.returncode)
            _owned_proc = None
            _started_by_us = False
            _ready_announced = False
        if not _started_by_us:
            if not _SERVER.exists():
                return False, f"找不到服务程序：{_SERVER}", False
            model = _find_model()
            if model is None:
                return False, "未找到本地模型（models\\hy-mt2 下缺少 .gguf）", False
            try:
                proc = _launch_owned(model)
            except Exception as e:
                logger.warning("启动 llama-server 失败: %s", e)
                return False, f"启动本地服务失败：{e}", False
            _owned_proc = proc
            _started_by_us = True
            launcher = True
            logger.info("已拉起 llama-server（pid=%s）", proc.pid)

    # 启动路径：轮询直到就绪或超时，期间定期回调进度
    last_report = 0.0
    deadline = start_time + timeout
    last_err = f"服务未在预期时间内就绪（首次加载模型通常需 10~60 秒，当前上限 {timeout:.0f} 秒）"
    while time.time() < deadline:
        if _probe():
            first = not _ready_announced
            _ready_announced = True
            return True, "本地服务已就绪", first
        proc = _owned_proc
        if proc is not None and proc.poll() is not None:
            last_err = f"本地服务进程异常退出（退出码 {proc.returncode}）"
            crashed = True
            break
        if on_progress:
            now = time.time()
            if now - last_report >= _PROGRESS_INTERVAL:
                last_report = now
                try:
                    on_progress(int(now - start_time))
                except Exception:
                    pass
        time.sleep(_POLL_INTERVAL)

    # 超时/失败：仅"实际拉起者"有权终止共享服务并重置状态。
    # 并发等待者各自超时是常态（模型加载慢），若任意等待者超时都能杀进程，
    # 会反复杀掉大家正在等待的 llama-server，造成"杀-起"抖动、全部翻译失败。
    if launcher:
        proc = _owned_proc
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except Exception:
                pass
        # 无论进程是被终止还是已自行退出（崩溃），都清空共享状态，保证可重试
        _owned_proc = None
        _started_by_us = False
    if crashed:
        diag = _startup_diagnosis()
        if diag:
            # 已有日志级真实原因时不再追加笼统猜测，避免误导
            return False, last_err + diag, False
    return False, last_err + "。可能原因：显存不足、模型损坏、或端口被占用。请先关闭已运行的 llama-server 后重试。", False


def is_service_running() -> bool:
    """本地翻译服务当前是否就绪（供其它模块探测；不触发启动、不等待）"""
    return _probe()


def shutdown_owned() -> None:
    """关闭本会话拉起的 llama-server；terminate 无效时 taskkill 兜底，避免残留"""
    global _owned_proc, _started_by_us
    proc = _owned_proc
    if proc is not None and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except Exception:
            pass
        if proc.poll() is None:  # terminate 未生效（如进程挂起）→ 强杀
            try:
                subprocess.run(["taskkill", "/F", "/PID", str(proc.pid)],
                               capture_output=True, text=True, timeout=3)
            except Exception as e:
                logger.warning("强制结束 llama-server %s 失败: %s", proc.pid, e)
    _owned_proc = None
    _started_by_us = False
