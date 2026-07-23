#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MKV 字幕软内嵌模块
"""
import logging
import os
import subprocess
from pathlib import Path
from typing import Callable, Optional, Tuple

from .config import cfg
from .srt_utils import parse_srt, write_srt, sanitize_blocks

logger = logging.getLogger(__name__)


def _sanitize_srt_for_mux(srt_path: Path) -> Path:
    """内嵌前净化 SRT 时间戳，作为转写端净化的兜底（应对手动/历史字幕文件）。

    返回净化后的临时 SRT 路径（与源同目录，调用方负责清理）。"""
    try:
        blocks = parse_srt(srt_path)
    except (OSError, ValueError, RuntimeError) as e:
        logger.warning("解析 SRT 失败，跳过净化: %s", e)
        return srt_path
    if not blocks:
        return srt_path
    before = [(b.start, b.end) for b in blocks]
    sanitize_blocks(blocks)
    if all(a == c and b == d for (a, b), (c, d) in zip(before, [(b.start, b.end) for b in blocks])):
        return srt_path
    out = srt_path.parent / (srt_path.stem + ".mux.sanitized.srt")
    write_srt(out, blocks, [b.text for b in blocks])
    return out


_MKV_MIN_SIZE = 1024  # 有效 MKV 最小字节数


def _find_sibling_probe(ffmpeg_bin: str) -> Optional[str]:
    """在 ffmpeg 同目录查找 ffprobe"""
    parent = Path(ffmpeg_bin).parent
    for name in ("ffprobe.exe", "ffprobe"):
        p = parent / name
        if p.exists():
            return str(p)
    return None


def _count_existing_sub_streams(video_path: Path, ffprobe_bin: Optional[str]) -> int:
    """用 ffprobe 统计视频文件已有的字幕流数量"""
    if not ffprobe_bin:
        return 0
    try:
        r = subprocess.run(
            [ffprobe_bin, "-v", "error", "-select_streams", "s",
             "-show_entries", "stream=index", "-of", "csv=p=0",
             str(video_path)],
            capture_output=True, text=True, timeout=30)
        if r.returncode == 0:
            return len([s for s in r.stdout.strip().split("\n") if s.strip()])
    except (subprocess.TimeoutExpired, OSError, ValueError) as e:
        logger.debug("统计字幕流失败: %s", e)
    return 0


def _probe_duration(path: Path, ffprobe_bin: Optional[str]) -> Optional[float]:
    """用 ffprobe 获取文件时长（秒），失败返回 None"""
    if not ffprobe_bin or not path.exists():
        return None
    try:
        r = subprocess.run(
            [ffprobe_bin, "-v", "error",
             "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1",
             str(path)],
            capture_output=True, text=True, timeout=30)
        if r.returncode == 0:
            val = r.stdout.strip()
            if val and val != "N/A":
                return float(val)
    except (subprocess.TimeoutExpired, OSError, ValueError) as e:
        logger.debug("探测时长失败 %s: %s", path, e)
    return None


def _verify_duration(video_path: Path, mkv_path: Path,
                     ffmpeg_bin: str) -> Tuple[bool, str]:
    """验证输出 MKV 时长，返回 (passed, detail_message)。

    passed=True 表示时长验证通过（ratio>=0.95 或源 <10s 跳过），可以安全删除原文件。
    passed=False 表示时长异常或无法验证，严禁删除原文件。
    """
    ffprobe = _find_sibling_probe(ffmpeg_bin)
    if not ffprobe:
        return False, "无法找到 ffprobe，跳过时长验证"
    src_dur = _probe_duration(video_path, ffprobe)
    out_dur = _probe_duration(mkv_path, ffprobe)
    if not src_dur or not out_dur:
        return False, (
            f"时长验证失败：无法获取时长"
            f"（源={'✓' if src_dur else '✗'} 输出={'✓' if out_dur else '✗'}）"
        )
    if src_dur < 10:
        return True, f"源视频过短（{src_dur:.1f}s），跳过时长验证"
    ratio = out_dur / src_dur
    if ratio < 0.95:
        return False, (
            f"输出时长 {out_dur:.1f}s 仅为源 {src_dur:.1f}s "
            f"的 {ratio*100:.0f}%，可能存在时间戳不连续"
        )
    return True, f"时长验证通过：{out_dur:.1f}s/{src_dur:.1f}s ({ratio*100:.0f}%)"


def _build_embed_cmd(ffmpeg_bin: str, video_path: Path, srt_path: Path,
                     mkv_path: Path) -> list[str]:
    """构建内嵌 ffmpeg 命令（主命令，最兼容）

    使用 -map 0:v? -map 0:a? -map 1 选择性映射视频/音频（optional）+ SRT。
    不映射源字幕流（-map 0:s?），因为 .ts 的 DVB/teletext 字幕流经常损坏。
    不映射 data/attachment 流，避免 "received no packets" 错误。

    防 .ts 时长截断措施：
    - 不加 -copyts：.ts 的原始时间戳经常全 0 或不连续，-copyts 会保留这些
      烂时间戳导致 MKV muxer 只写出几百帧就失败。让 ffmpeg 用默认行为重算时间戳。
    - -avoid_negative_ts make_zero：确保输出时间戳从 0 开始
    - 明确不加 -shortest 和 -fflags +discardcorrupt
    """
    return [ffmpeg_bin, "-y",
            "-i", str(video_path), "-i", str(srt_path),
            "-map", "0:v?", "-map", "0:a?", "-map", "1",
            "-c:v", "copy", "-c:a", "copy",
            "-c:s", "srt",
            "-avoid_negative_ts", "make_zero",
            "-metadata:s:s:0", f"language={cfg.translation.embed_subtitle_lang}",
            "-disposition:s:0", "default",
            str(mkv_path)]


def _remux_ts_to_mkv(ffmpeg_bin: str, ts_path: Path, post: Callable,
                     register_proc=None, unregister_proc=None) -> Optional[Path]:
    """对 .ts 文件做修复性重封装为临时 .mkv

    .ts 文件的 PCR/PTS 经常全 0 或不连续，直接内嵌会导致输出严重截断。
    先用 -fflags +genpts -err_detect ignore_err 重封装为 .mkv：
    - +genpts：补全缺失 PTS
    - ignore_err：遇错误包不中断，尽量多读数据
    重封装后 ffprobe 报告的时长可能不准（时间戳仍可能异常），
    但实际视频/音频包已全部复制，内嵌步骤会重算时间戳恢复真实时长。
    返回临时 .mkv 路径，调用方负责清理。
    """
    mkv_temp = ts_path.with_suffix(".remux.tmp.mkv")
    if mkv_temp.exists():
        mkv_temp = ts_path.parent / f"{ts_path.stem}.remux.{os.getpid()}.tmp.mkv"

    cmd = [ffmpeg_bin, "-y",
           "-fflags", "+genpts",
           "-err_detect", "ignore_err",
           "-i", str(ts_path),
           "-map", "0:v?", "-map", "0:a?",
           "-c:v", "copy", "-c:a", "copy",
           "-avoid_negative_ts", "make_zero",
           str(mkv_temp)]

    post({"type": "log", "message": "修复性重封装 .ts → .mkv（解决时间戳问题）...", "level": "INFO"})
    result = _run_ffmpeg(cmd, post, cfg.translation.embed_timeout,
                         register_proc, unregister_proc)

    if result is None:
        if mkv_temp.exists() and mkv_temp.stat().st_size > _MKV_MIN_SIZE:
            ffprobe = _find_sibling_probe(ffmpeg_bin)
            dur = _probe_duration(mkv_temp, ffprobe)
            if dur and dur > 10:
                post({"type": "log",
                      "message": f"重封装完成（超时但已生成）：ffprobe 报告 {dur:.0f}s（实际数据可能更完整）",
                      "level": "INFO"})
                return mkv_temp
        if mkv_temp.exists():
            mkv_temp.unlink(missing_ok=True)
        return None

    proc, _, stderr = result

    if proc.returncode == 0 or (mkv_temp.exists() and mkv_temp.stat().st_size > _MKV_MIN_SIZE):
        ffprobe = _find_sibling_probe(ffmpeg_bin)
        fixed_dur = _probe_duration(mkv_temp, ffprobe)
        src_dur = _probe_duration(ts_path, ffprobe)
        if fixed_dur and src_dur and fixed_dur > src_dur * 0.5:
            post({"type": "log",
                  "message": f"重封装完成：{fixed_dur:.0f}s（源 {src_dur:.0f}s，{fixed_dur / src_dur * 100:.0f}%）",
                  "level": "INFO"})
            return mkv_temp
        if fixed_dur:
            post({"type": "log",
                  "message": f"重封装完成：ffprobe 报告 {fixed_dur:.0f}s（源 {src_dur:.0f}s），"
                             f"时间戳仍异常但数据已复制，内嵌时会重算",
                  "level": "INFO"})
            return mkv_temp
        post({"type": "log", "message": "重封装后无法读取时长，继续尝试内嵌", "level": "WARNING"})
        return mkv_temp

    _log_ffmpeg_error(post, " ".join(cmd), proc, stderr)
    if mkv_temp.exists():
        mkv_temp.unlink(missing_ok=True)
    return None


def _run_ffmpeg(cmd: list[str], post: Callable, timeout: int,
                register_proc=None, unregister_proc=None):
    """执行 ffmpeg，返回 (proc, stdout, stderr) 或 None（超时/异常）"""
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, encoding="utf-8", errors="replace",
                                creationflags=subprocess.CREATE_NO_WINDOW)
    except (OSError, ValueError) as e:
        post({"type": "log", "message": f"启动 ffmpeg 失败: {e}", "level": "ERROR"})
        return None
    if register_proc:
        register_proc(proc)
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        return proc, stdout, stderr
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        post({"type": "log", "message": f"内嵌字幕超时（ffmpeg 超过 {timeout}s，不影响字幕文件）", "level": "WARNING"})
        return None
    finally:
        if unregister_proc:
            unregister_proc(proc)


def embed_subtitles_to_video(video_path: Path, srt_path: Path, ffmpeg_bin: str, post: Callable,
                              register_proc=None, unregister_proc=None) -> Tuple[Optional[Path], bool]:
    """将 SRT 软内嵌到 MKV 容器（不重编码视频/音频）。

    返回 (mkv_path, is_trustworthy)：
    - mkv_path: MKV 路径，None 表示完全失败
    - is_trustworthy: True 表示时长验证通过，可以安全删除原文件；
                      False 表示异常（时长不符或无法验证），严禁删除原文件

    流程：
    1. 净化 SRT 时间戳
    2. 若源为 .ts，先修复性重封装为临时 .mkv（解决时间戳全 0/不连续）
    3. 执行 ffmpeg 软内嵌（在修复后的 .mkv 或原始文件上）
    4. 若 ffmpeg 返回非零但 MKV 已生成且有效，仍视为成功
    5. 若 MKV 无效或无输出，尝试降级命令重试
    6. 时长验证并返回可信度
    """
    if not video_path.exists() or not srt_path.exists():
        return None

    sanitized = None
    remux_temp = None

    try:
        srt_for_mux = _sanitize_srt_for_mux(srt_path)
        if srt_for_mux is not srt_path:
            sanitized = srt_for_mux

        # ── .ts 文件先修复性重封装 ──
        actual_video = video_path
        if video_path.suffix.lower() == ".ts":
            remux_temp = _remux_ts_to_mkv(ffmpeg_bin, video_path, post,
                                          register_proc, unregister_proc)
            if remux_temp:
                actual_video = remux_temp
            else:
                post({"type": "log", "message": "重封装失败，尝试直接内嵌原始 .ts", "level": "WARNING"})

        # 输出文件名始终基于原始视频名，不基于 remux 临时文件
        mkv_path = video_path.with_suffix(".mkv")
        if mkv_path.exists():
            mkv_path = video_path.parent / f"{video_path.stem}_subbed.mkv"

        cmd = _build_embed_cmd(ffmpeg_bin, actual_video, srt_for_mux, mkv_path)
        cmd_str = " ".join(str(a) for a in cmd)
        post({"type": "log", "message": f"内嵌字幕到 {mkv_path.name}...", "level": "INFO"})

        result = _run_ffmpeg(cmd, post, cfg.translation.embed_timeout,
                             register_proc, unregister_proc)

        if result is None:
            _cleanup_file(sanitized)
            _cleanup_file(remux_temp)
            if mkv_path and mkv_path.exists() and mkv_path.stat().st_size > _MKV_MIN_SIZE:
                dur_ok, dur_msg = _verify_duration(video_path, mkv_path, ffmpeg_bin)
                post({"type": "log", "message": f"内嵌超时但 MKV 已生成，{dur_msg}", "level": "WARNING"})
                return mkv_path, dur_ok
            return None, False

        proc, stdout, stderr = result

        if proc.returncode == 0:
            post({"type": "log", "message": f"内嵌字幕完成：{mkv_path.name}", "level": "INFO"})
            dur_ok, dur_msg = _verify_duration(video_path, mkv_path, ffmpeg_bin)
            if dur_ok:
                post({"type": "log", "message": dur_msg, "level": "INFO"})
            else:
                post({"type": "log", "message": f"⚠ {dur_msg}", "level": "WARNING"})
            _cleanup_file(sanitized)
            _cleanup_file(remux_temp)
            return mkv_path, dur_ok

        # ── ffmpeg 返回非零 ──
        _log_ffmpeg_error(post, cmd_str, proc, stderr)

        # ffmpeg 返回非零但 MKV 已有效生成 → 容忍
        if mkv_path.exists() and mkv_path.stat().st_size > _MKV_MIN_SIZE:
            post({"type": "log", "message": f"MKV 文件已有效生成（{mkv_path.stat().st_size / 1024:.0f}KiB），仍视为成功",
                  "level": "INFO"})
            dur_ok, dur_msg = _verify_duration(video_path, mkv_path, ffmpeg_bin)
            if dur_ok:
                post({"type": "log", "message": dur_msg, "level": "INFO"})
            else:
                post({"type": "log", "message": f"⚠ {dur_msg}", "level": "WARNING"})
            _cleanup_file(sanitized)
            _cleanup_file(remux_temp)
            return mkv_path, dur_ok

        # ── 降级重试：加 -fflags +genpts ──
        post({"type": "log", "message": "尝试降级命令重试（+genpts）...", "level": "INFO"})
        fallback_cmd = [ffmpeg_bin, "-y",
                        "-fflags", "+genpts",
                        "-i", str(actual_video), "-i", str(srt_for_mux),
                        "-map", "0:v?", "-map", "0:a?", "-map", "1",
                        "-c:v", "copy", "-c:a", "copy",
                        "-c:s", "srt",
                        "-avoid_negative_ts", "make_zero",
                        str(mkv_path)]
        result2 = _run_ffmpeg(fallback_cmd, post, cfg.translation.embed_timeout,
                              register_proc, unregister_proc)
        if result2 is None:
            _cleanup_file(sanitized)
            _cleanup_file(remux_temp)
            if mkv_path and mkv_path.exists() and mkv_path.stat().st_size > _MKV_MIN_SIZE:
                dur_ok, dur_msg = _verify_duration(video_path, mkv_path, ffmpeg_bin)
                post({"type": "log", "message": f"降级重试超时但 MKV 已生成，{dur_msg}", "level": "WARNING"})
                return mkv_path, dur_ok
            return None, False

        proc2, _, stderr2 = result2
        if proc2.returncode == 0 or (mkv_path.exists() and mkv_path.stat().st_size > _MKV_MIN_SIZE):
            if proc2.returncode == 0:
                post({"type": "log", "message": f"降级命令内嵌完成：{mkv_path.name}", "level": "INFO"})
            else:
                _log_ffmpeg_error(post, " ".join(str(a) for a in fallback_cmd), proc2, stderr2)
                post({"type": "log", "message": f"MKV 文件已有效生成（{mkv_path.stat().st_size / 1024:.0f}KiB），仍视为成功",
                      "level": "INFO"})
            dur_ok, dur_msg = _verify_duration(video_path, mkv_path, ffmpeg_bin)
            if dur_ok:
                post({"type": "log", "message": dur_msg, "level": "INFO"})
            else:
                post({"type": "log", "message": f"⚠ {dur_msg}", "level": "WARNING"})
            _cleanup_file(sanitized)
            _cleanup_file(remux_temp)
            return mkv_path, dur_ok

        # 两轮均失败且无有效输出 → 清理
        _log_ffmpeg_error(post, " ".join(str(a) for a in fallback_cmd), proc2, stderr2)
        _cleanup_file(mkv_path)

    except Exception as e:
        logger.error("内嵌字幕出错: %s", e)
        post({"type": "log", "message": f"内嵌字幕出错（不影响字幕文件）: {e}", "level": "WARNING"})

    _cleanup_file(sanitized)
    _cleanup_file(remux_temp)
    return None, False


def _cleanup_file(path) -> None:
    """清理临时文件（自动跳过 None 和不存在的情况）"""
    if path is not None:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def _log_ffmpeg_error(post: Callable, cmd_str: str, proc, stderr: str) -> None:
    """记录 ffmpeg 错误诊断信息"""
    stderr_clean = stderr.strip() if stderr else ""
    err_head = stderr_clean[:300] if stderr_clean else "无错误输出"
    err_tail = stderr_clean[-200:] if len(stderr_clean) > 300 else ""
    msg = f"ffmpeg 返回 {proc.returncode} | 命令: {cmd_str[:500]}"
    post({"type": "log", "message": msg, "level": "WARNING"})
    post({"type": "log", "message": f"stderr 开头: {err_head}", "level": "WARNING"})
    if err_tail:
        post({"type": "log", "message": f"stderr 末尾: {err_tail}", "level": "WARNING"})
