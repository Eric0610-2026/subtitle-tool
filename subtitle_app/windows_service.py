"""按本机翻译端点定位服务，并用同一 Windows 进程句柄核实身份和关闭。"""
import ctypes
import json
import os
import socket
import struct
import subprocess
from ctypes import wintypes
from pathlib import Path


class _TcpRow(ctypes.Structure):
    _fields_ = [(name, wintypes.DWORD) for name in
                ("state", "address", "port", "remote_address", "remote_port", "pid")]


class _Tcp6Row(ctypes.Structure):
    _fields_ = [("address", ctypes.c_ubyte * 16), ("scope", wintypes.DWORD),
                ("port", wintypes.DWORD), ("remote_address", ctypes.c_ubyte * 16),
                ("remote_scope", wintypes.DWORD), ("remote_port", wintypes.DWORD),
                ("state", wintypes.DWORD), ("pid", wintypes.DWORD)]


def _listening_pids(host: str, port: int) -> set:
    api = ctypes.WinDLL("iphlpapi", use_last_error=True).GetExtendedTcpTable
    api.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD), wintypes.BOOL,
                    wintypes.ULONG, ctypes.c_int, wintypes.ULONG]
    api.restype = wintypes.DWORD
    pids = set()
    for family, row_type in ((2, _TcpRow), (23, _Tcp6Row)):
        if family == 23 and pids:
            return pids  # 已有明确 IPv4 监听，不混入同端口的 IPv6-only 服务。
        size = wintypes.DWORD()
        result = api(None, ctypes.byref(size), False, family, 3, 0)
        if result not in (0, 122):
            raise ctypes.WinError(result)
        for attempt in range(3):
            buffer = ctypes.create_string_buffer(size.value)
            result = api(buffer, ctypes.byref(size), False, family, 3, 0)
            if result != 122:
                break
        if result:
            raise ctypes.WinError(result)
        count = wintypes.DWORD.from_buffer(buffer).value
        for index in range(count):
            row = row_type.from_buffer(buffer, ctypes.sizeof(wintypes.DWORD)
                                      + index * ctypes.sizeof(row_type))
            if row.state != 2 or socket.ntohs(row.port & 0xFFFF) != port:
                continue
            if family == 2:
                address = socket.inet_ntoa(struct.pack("<I", row.address))
                matches = address in (host, "0.0.0.0")
            else:
                # 调用者已核实 IPv4 端点可连接；仅通配 IPv6 监听可能承接它。
                matches = not any(row.address)
            if matches:
                pids.add(row.pid)
    return pids


class _ServiceProcess:
    def __init__(self, pid: int):
        self.pid = pid
        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        signatures = {
            "OpenProcess": ([wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
            "CloseHandle": ([wintypes.HANDLE], wintypes.BOOL),
            "QueryFullProcessImageNameW": ([wintypes.HANDLE, wintypes.DWORD,
                                           wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
            "GetProcessTimes": ([wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4, wintypes.BOOL),
            "TerminateProcess": ([wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
            "WaitForSingleObject": ([wintypes.HANDLE, wintypes.DWORD], wintypes.DWORD),
        }
        for name, (arguments, result) in signatures.items():
            getattr(self.api, name).argtypes = arguments
            getattr(self.api, name).restype = result
        self.handle = self.api.OpenProcess(0x1000 | 0x0001 | 0x100000, False, pid)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self):
        self.api.CloseHandle(self.handle)

    def image_path(self) -> Path:
        size = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not self.api.QueryFullProcessImageNameW(self.handle, 0, buffer, ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        return Path(buffer.value)

    def creation_ticks(self) -> int:
        created, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
        if not self.api.GetProcessTimes(self.handle, ctypes.byref(created), ctypes.byref(exited),
                                       ctypes.byref(kernel), ctypes.byref(user)):
            raise ctypes.WinError(ctypes.get_last_error())
        return (created.dwHighDateTime << 32) | created.dwLowDateTime

    def terminate(self) -> bool:
        if self.api.WaitForSingleObject(self.handle, 0) == 0:
            return True  # 在核实期间自行退出。
        if not self.api.TerminateProcess(self.handle, 0):
            raise ctypes.WinError(ctypes.get_last_error())
        result = self.api.WaitForSingleObject(self.handle, 3000)
        if result == 0xFFFFFFFF:
            raise ctypes.WinError(ctypes.get_last_error())
        return result == 0


def _process_metadata(pid: int) -> dict:
    powershell = Path(os.environ.get("SystemRoot", "C:/Windows")) / (
        "System32/WindowsPowerShell/v1.0/powershell.exe")
    script = (
        "$ErrorActionPreference='Stop'; "
        "[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new($false); "
        f"$serviceProcess=Get-CimInstance Win32_Process -Filter 'ProcessId = {int(pid)}'; "
        "if ($null -eq $serviceProcess) { throw 'Service process exited' }; "
        "@{command=$serviceProcess.CommandLine; "
        "created=$serviceProcess.CreationDate.ToUniversalTime().ToFileTimeUtc()} "
        "| ConvertTo-Json -Compress"
    )
    result = subprocess.run([str(powershell), "-NoProfile", "-NonInteractive", "-Command", script],
                            capture_output=True, timeout=5, encoding="utf-8", errors="replace",
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode:
        raise RuntimeError("无法读取服务进程信息，可能没有访问权限或进程已退出")
    return json.loads(result.stdout.lstrip("\ufeff"))


def _command_args(command: str) -> list:
    api = ctypes.WinDLL("shell32", use_last_error=True).CommandLineToArgvW
    api.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
    api.restype = ctypes.POINTER(wintypes.LPWSTR)
    free = ctypes.WinDLL("kernel32", use_last_error=True).LocalFree
    free.argtypes = [ctypes.c_void_p]
    free.restype = ctypes.c_void_p
    count = ctypes.c_int()
    arguments = api(command, ctypes.byref(count))
    if not arguments:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return [arguments[index] for index in range(count.value)]
    finally:
        free(arguments)


def _is_hy_model(arguments: list, models_dir: Path) -> bool:
    model = None
    for index, argument in enumerate(arguments):
        if argument in ("-m", "--model") and index + 1 < len(arguments):
            model = arguments[index + 1]
        elif argument.startswith("--model="):
            model = argument.split("=", 1)[1]
    if not model:
        return False
    path = Path(model)
    if not path.is_absolute() or path.suffix.lower() != ".gguf":
        return False
    return path.resolve().is_relative_to(models_dir.resolve())


def stop_external_service(host: str, port: int, server: Path, models_dir: Path) -> tuple:
    """关闭经过身份核实的服务；核实失败时不结束任何进程。"""
    if os.name != "nt":
        return False, "当前平台不支持关闭外部启动的本地服务"
    process = None
    try:
        pids = _listening_pids(host, port)
        if not pids:
            return False, "翻译端口有连接，但无法确认对应的服务进程"
        if len(pids) != 1:
            return False, "翻译端口对应多个进程，无法确认要关闭的服务"
        pid = next(iter(pids))
        process = _ServiceProcess(pid)
        image = process.image_path().resolve()
        if image != server.resolve():
            return False, "翻译端口被其它程序占用，未关闭该进程"
        metadata = _process_metadata(pid)
        if not metadata.get("command") or not _is_hy_model(_command_args(metadata["command"]), models_dir):
            return False, "无法确认该服务使用 Hy-MT2 模型，未关闭该进程"
        if process.creation_ticks() // 10 != int(metadata["created"]) // 10:
            return False, "服务进程身份已变化，请刷新后重试"
        if _listening_pids(host, port) != {pid}:
            return False, "翻译端口对应的进程已变化，请刷新后重试"
        if not process.terminate():
            return False, "Hy-MT2 服务进程仍在运行，请重试卸下"
        if _listening_pids(host, port):
            return False, "原服务已关闭，但翻译端口被新的进程占用"
        return True, "Hy-MT2 已卸下"
    except Exception as exc:
        return False, f"关闭 Hy-MT2 失败：{exc}"
    finally:
        if process is not None:
            process.close()
