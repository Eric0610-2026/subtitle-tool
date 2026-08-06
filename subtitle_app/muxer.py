#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MKV 字幕软内嵌模块
"""
import json
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
    after = [(b.start, b.end) for b in blocks]
    # 先比较块数：sanitize_blocks 会就地过滤空文本块，块数可能减少；
    # 若只用 zip 比较公共前缀，末尾空块被过滤时会被误判为"未变化"而跳过净化。
    if len(before) == len(after) and all(a == c and b == d for (a, b), (c, d) in zip(before, after)):
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
        if r.returncode != 0:
            logger.debug("统计字幕流失败 %s: ffprobe 退出码 %s", video_path, r.returncode)
            return 0
        # split() 按任意空白分割并自动忽略空串/空白行（等价于原
        # strip().split("\n") + 过滤写法，且对空输出直接得到 0）
        return len(r.stdout.split())
    except (subprocess.TimeoutExpired, OSError, ValueError) as e:
        logger.debug("统计字幕流失败 %s: %s", video_path, e)
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
                register_proc=None, unregister_proc=None,
                timeout_msg: str = None):
    """执行 ffmpeg，返回 (proc, stdout, stderr) 或 None（超时/异常）

    timeout_msg: 自定义超时提示文案（默认「内嵌字幕超时...」，供转换等
    非内嵌场景传更贴切的描述）。
    """
    if timeout_msg is None:
        timeout_msg = "内嵌字幕超时（ffmpeg 超过 {}s，不影响字幕文件）".format(timeout)
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
        post({"type": "log", "message": timeout_msg, "level": "WARNING"})
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
        # 与其它失败路径一致，返回二元组，避免调用方
        # `mkv_path, mkv_trusted = embed_subtitles_to_video(...)` 解包 TypeError
        return None, False

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
                return _verify_and_return(video_path, mkv_path, ffmpeg_bin, sanitized, remux_temp, post,
                                          "内嵌超时但 MKV 已生成", "WARNING")
            return None, False

        proc, stdout, stderr = result

        if proc.returncode == 0:
            _cleanup_file(sanitized)
            _cleanup_file(remux_temp)
            return _verify_and_return(video_path, mkv_path, ffmpeg_bin, sanitized, remux_temp, post,
                                      f"内嵌字幕完成：{mkv_path.name}")

        # ── ffmpeg 返回非零 ──
        _log_ffmpeg_error(post, cmd_str, proc, stderr)

        # ffmpeg 返回非零但 MKV 已有效生成 → 容忍
        if mkv_path.exists() and mkv_path.stat().st_size > _MKV_MIN_SIZE:
            _cleanup_file(sanitized)
            _cleanup_file(remux_temp)
            return _verify_and_return(video_path, mkv_path, ffmpeg_bin, sanitized, remux_temp, post,
                                      f"MKV 文件已有效生成（{mkv_path.stat().st_size / 1024:.0f}KiB），仍视为成功")

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
                return _verify_and_return(video_path, mkv_path, ffmpeg_bin, sanitized, remux_temp, post,
                                          "降级重试超时但 MKV 已生成", "WARNING")
            return None, False

        proc2, _, stderr2 = result2
        if proc2.returncode == 0 or (mkv_path.exists() and mkv_path.stat().st_size > _MKV_MIN_SIZE):
            if proc2.returncode != 0:
                _log_ffmpeg_error(post, " ".join(str(a) for a in fallback_cmd), proc2, stderr2)
            _cleanup_file(sanitized)
            _cleanup_file(remux_temp)
            return _verify_and_return(video_path, mkv_path, ffmpeg_bin, sanitized, remux_temp, post,
                                      f"降级命令内嵌完成：{mkv_path.name}")

        # 两轮均失败且无有效输出 → 清理
        _log_ffmpeg_error(post, " ".join(str(a) for a in fallback_cmd), proc2, stderr2)
        _cleanup_file(mkv_path)

    except Exception as e:
        logger.error("内嵌字幕出错: %s", e)
        post({"type": "log", "message": f"内嵌字幕出错（不影响字幕文件）: {e}", "level": "WARNING"})

    _cleanup_file(sanitized)
    _cleanup_file(remux_temp)
    return None, False


def _verify_and_return(video_path, mkv_path, ffmpeg_bin, sanitized, remux_temp, post, log_msg, level="INFO"):
    """验证时长 + 清理临时文件 + 返回"""
    dur_ok, dur_msg = _verify_duration(video_path, mkv_path, ffmpeg_bin)
    post({"type": "log", "message": log_msg, "level": level})
    post({"type": "log", "message": dur_msg, "level": "INFO" if dur_ok else "WARNING"})
    _cleanup_file(sanitized)
    _cleanup_file(remux_temp)
    return mkv_path, dur_ok


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


# ── 从 MKV 提取内嵌字幕 ──

# 可无损转换为 SRT 的文本字幕编码（ffmpeg `-c:s srt` 可转换）；
# 图像字幕（PGS/DVD/VobSub 等）无法转为文本 SRT。
_TEXT_SUB_CODECS = {"subrip", "srt", "ass", "ssa", "webvtt", "mov_text", "text", "eia_608", "eia_708"}


def _probe_first_sub_stream(ffprobe_bin: Optional[str], video_path: Path) -> Optional[dict]:
    """探测视频第一个字幕流的 (index, codec_name, language)。

    返回 dict 或 None（无 ffprobe / 无字幕流 / 探测失败）。
    """
    if not ffprobe_bin:
        return None
    try:
        r = subprocess.run(
            [ffprobe_bin, "-v", "error",
             "-select_streams", "s:0",
             "-show_entries", "stream=index,codec_name:stream_tags=language",
             "-of", "json",
             str(video_path)],
            capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            return None
        data = json.loads(r.stdout)
        streams = (data or {}).get("streams") or []
        if not streams:
            return None
        s = streams[0]
        tags = s.get("tags") or {}
        return {
            "index": s.get("index", 0),
            "codec_name": s.get("codec_name", ""),
            "language": tags.get("language", ""),
        }
    except (subprocess.TimeoutExpired, OSError, ValueError) as e:
        logger.debug("探测字幕流失败 %s: %s", video_path, e)
        return None


def extract_embedded_subtitle(video_path: Path, ffmpeg_bin: str, post: Callable,
                              register_proc=None, unregister_proc=None) -> Tuple[Optional[Path], str]:
    """从视频文件（通常是 MKV）提取第一个字幕流为独立 SRT 文件。

    返回 (srt_path, 状态描述)：
    - srt_path: 提取成功的 SRT 路径；None 表示失败（无字幕流/图像字幕/ffmpeg 失败）
    - 状态描述: 供日志展示的人类可读信息

    仅提取第一个（默认）字幕流；图像字幕（PGS/VobSub 等）无法转为文本 SRT，
    会以失败返回并说明原因。
    """
    if not video_path.exists():
        return None, "视频文件不存在"

    ffprobe = _find_sibling_probe(ffmpeg_bin)
    stream_info = _probe_first_sub_stream(ffprobe, video_path)
    if stream_info is None:
        # 探测失败或没有 ffprobe：仍尝试直接提取，让 ffmpeg 给出最终判断
        post({"type": "log",
              "message": "未探测到字幕流信息（或缺少 ffprobe），尝试直接提取...",
              "level": "INFO"})
    else:
        codec = stream_info.get("codec_name", "")
        if codec and codec not in _TEXT_SUB_CODECS:
            return None, (
                f"第一个字幕流为图像字幕（{codec}），无法提取为文本 SRT"
            )

    # 输出文件名：优先同名 .srt，已存在则依次加 _extracted/_extracted2... 避免覆盖
    out_srt = video_path.with_suffix(".srt")
    if out_srt.exists():
        stem = video_path.stem
        n = 1
        while (video_path.parent / f"{stem}_extracted{n}.srt").exists():
            n += 1
        out_srt = video_path.parent / f"{stem}_extracted{n}.srt"

    lang = (stream_info or {}).get("language", "")
    cmd = [ffmpeg_bin, "-y",
           "-i", str(video_path),
           "-map", "0:s:0",
           "-c:s", "srt",
           str(out_srt)]
    cmd_str = " ".join(str(a) for a in cmd)
    lang_desc = f"（语言: {lang}）" if lang else ""
    post({"type": "log", "message": f"提取字幕流到 {out_srt.name} {lang_desc}...", "level": "INFO"})

    result = _run_ffmpeg(cmd, post, cfg.translation.embed_timeout,
                         register_proc, unregister_proc,
                         timeout_msg=f"提取字幕超时（ffmpeg 超过 {cfg.translation.embed_timeout}s）")
    if result is None:
        if out_srt.exists() and out_srt.stat().st_size > 0:
            return out_srt, "提取超时但 SRT 已生成"
        return None, "提取超时，未生成 SRT 文件"

    proc, _, stderr = result
    if proc.returncode == 0 and out_srt.exists() and out_srt.stat().st_size > 0:
        return out_srt, f"提取完成: {out_srt.name}"

    _log_ffmpeg_error(post, cmd_str, proc, stderr)
    # ffmpeg 返回非零但 SRT 已生成 → 容忍（部分流可能被截断）
    if out_srt.exists() and out_srt.stat().st_size > 0:
        return out_srt, f"ffmpeg 返回非零但 SRT 已生成: {out_srt.name}"
    if out_srt.exists():
        out_srt.unlink(missing_ok=True)
    return None, "提取失败，请查看日志"


# ── MKV → MP4 转换（不带字幕）──

_MP4_MIN_SIZE = 1024  # 有效 MP4 最小字节数


def _build_mp4_cmd(ffmpeg_bin: str, video_path: Path, mp4_path: Path,
                   video_codec: str = "copy", audio_codec: str = "copy") -> list[str]:
    """构建 MKV → MP4 转换命令：只带视频/音频流，不带字幕。

    MP4 容器对部分编码不兼容（如 DTS 音频、部分 PCM），故 audio_codec
    可降级为 aac；video_codec 可降级为 libx264（最兼容）。
    -map 0:v? -map 0:a?：仅映射视频/音频（可选），排除字幕/附件流
    -sn：显式排除字幕流
    -movflags +faststart：MP4 元数据前置，便于边下边播
    """
    return [ffmpeg_bin, "-y",
            "-i", str(video_path),
            "-map", "0:v?", "-map", "0:a?",
            "-c:v", video_codec, "-c:a", audio_codec,
            "-sn",
            "-movflags", "+faststart",
            str(mp4_path)]


def _next_mp4_path(video_path: Path) -> Path:
    """生成不冲突的 MP4 输出路径：优先同名 .mp4，已存在则 _convertedN.mp4"""
    out = video_path.with_suffix(".mp4")
    if not out.exists():
        return out
    stem = video_path.stem
    n = 1
    while (video_path.parent / f"{stem}_converted{n}.mp4").exists():
        n += 1
    return video_path.parent / f"{stem}_converted{n}.mp4"


def _strip_mp4_subs(video_path: Path, ffmpeg_bin: str, post: Callable,
                    register_proc=None, unregister_proc=None) -> Tuple[Optional[Path], bool]:
    """源已是 MP4：若带内嵌字幕（mov_text 等），重封装为无字幕 MP4 并原子替换原文件。

    返回 (video_path, True)：替换成功且时长验证通过（原文件已为无字幕版本）；
    返回 (None, False)：失败/验证不通过/替换失败（原文件保留不动）。
    无内嵌字幕时直接返回 (video_path, True) 跳过，不产生任何写入。
    """
    ffprobe = _find_sibling_probe(ffmpeg_bin)
    if _count_existing_sub_streams(video_path, ffprobe) == 0:
        post({"type": "log", "message": "源文件已是 MP4 且无内嵌字幕，跳过转换", "level": "INFO"})
        return video_path, True

    # MP4 源无需 libx264 全重编码：视频/音频必然兼容 MP4 容器，流复制即可
    temp = video_path.parent / f"{video_path.stem}.nostsub.{os.getpid()}.tmp.mp4"
    attempts = [
        ("copy", "copy", "流复制", cfg.translation.embed_timeout),
        ("copy", "aac", "音频重编码 (aac)", cfg.translation.embed_timeout),
    ]
    for vcodec, acodec, desc, timeout in attempts:
        cmd = _build_mp4_cmd(ffmpeg_bin, video_path, temp, vcodec, acodec)
        cmd_str = " ".join(str(a) for a in cmd)
        post({"type": "log", "message": f"去除内嵌字幕（{desc}）...", "level": "INFO"})

        result = _run_ffmpeg(cmd, post, timeout, register_proc, unregister_proc,
                             timeout_msg=f"去字幕转换超时（ffmpeg 超过 {timeout}s）")
        if result is None:
            _cleanup_file(temp)
            continue

        proc, _, stderr = result
        if proc.returncode == 0 and temp.exists() and temp.stat().st_size > _MP4_MIN_SIZE:
            ok, detail = _verify_duration(video_path, temp, ffmpeg_bin)
            post({"type": "log", "message": f"{desc}输出 {temp.name} | {detail}",
                  "level": "WARNING" if not ok else "INFO"})
            if ok:
                try:
                    os.replace(temp, video_path)
                except OSError as e:
                    post({"type": "log",
                          "message": f"替换原文件失败: {e}（保留原文件）", "level": "WARNING"})
                    _cleanup_file(temp)
                    return None, False
                post({"type": "log",
                      "message": f"✅ 已移除内嵌字幕并替换原文件: {video_path.name}", "level": "INFO"})
                return video_path, True
            # 时长验证失败：继续降级尝试（重编码可能修复容器兼容问题）
            post({"type": "log", "message": "时长验证未通过，尝试更高兼容性转换...", "level": "WARNING"})
        else:
            _log_ffmpeg_error(post, cmd_str, proc, stderr)
        _cleanup_file(temp)
    return None, False


def convert_to_mp4(video_path: Path, ffmpeg_bin: str, post: Callable,
                   register_proc=None, unregister_proc=None) -> Tuple[Optional[Path], bool]:
    """将视频（通常是 MKV）转换为 MP4，仅保留视频/音频流（不带字幕）。

    返回 (mp4_path, is_trustworthy)：
    - mp4_path: MP4 路径；None 表示完全失败
    - is_trustworthy: True 表示时长验证通过，可以安全删除原文件；
                      False 表示异常（时长不符或无法验证），严禁删除原文件

    源文件本身就是 .mp4 时走 _strip_mp4_subs：无内嵌字幕则跳过，直接返回
    (video_path, True)；带内嵌字幕则去字幕重封装并替换原文件，同样返回
    (video_path, True)。调用方在删除前必须确认 mp4 与源不是同一文件
    （防止误删用户原文件）。

    流程：
    1. 主命令：流复制（-c:v copy -c:a copy）+ faststart
    2. 若失败：降级音频重编码（-c:a aac，兼容 MP4 容器不支持的音频编码）
    3. 若仍失败：视频/音频全重编码（libx264 + aac，最兼容但慢）
    每级成功后都做时长验证；验证失败则继续下一级尝试（重编码可能修复
    容器兼容问题），全部尝试完仍无有效输出才算失败。
    """
    if not video_path.exists():
        return None, False
    if video_path.suffix.lower() == ".mp4":
        return _strip_mp4_subs(video_path, ffmpeg_bin, post,
                               register_proc, unregister_proc)

    mp4_path = _next_mp4_path(video_path)

    attempts = [
        ("copy", "copy", "流复制", cfg.translation.embed_timeout),
        ("copy", "aac", "音频重编码 (aac)", cfg.translation.embed_timeout),
        ("libx264", "aac", "全重编码 (libx264+aac)", cfg.translation.embed_timeout * 3),
    ]

    best = None  # (mp4_path, is_trustworthy)，记录最后有效候选
    for i, (vcodec, acodec, desc, timeout) in enumerate(attempts):
        cmd = _build_mp4_cmd(ffmpeg_bin, video_path, mp4_path, vcodec, acodec)
        cmd_str = " ".join(str(a) for a in cmd)
        post({"type": "log", "message": f"转换 {video_path.name} → {mp4_path.name}（{desc}）...", "level": "INFO"})

        result = _run_ffmpeg(cmd, post, timeout,
                             register_proc, unregister_proc,
                             timeout_msg=f"转换超时（ffmpeg 超过 {timeout}s）")
        if result is None:
            post({"type": "log", "message": f"第 {i+1} 次尝试超时", "level": "WARNING"})
            _cleanup_file(mp4_path)
            continue

        proc, _, stderr = result
        if proc.returncode == 0 and mp4_path.exists() and mp4_path.stat().st_size > _MP4_MIN_SIZE:
            ok, detail = _verify_duration(video_path, mp4_path, ffmpeg_bin)
            post({"type": "log", "message": f"第 {i+1} 次尝试输出 {mp4_path.name} | {detail}",
                  "level": "WARNING" if not ok else "INFO"})
            if ok:
                return mp4_path, True
            # 时长验证失败：记录候选并继续下一级（重编码可能修复）
            best = (mp4_path, False)
            post({"type": "log",
                  "message": "时长验证未通过，继续尝试更高兼容性转换...", "level": "WARNING"})
            continue

        _log_ffmpeg_error(post, cmd_str, proc, stderr)
        _cleanup_file(mp4_path)

    if best:
        best_path, best_ok = best
        # 后续级别的尝试可能超时并清理了 best 文件，返回前确认仍存在
        if not best_path.exists():
            return None, False
        post({"type": "log",
              "message": "所有级别均未通过时长验证，保留原文件（禁止删除）", "level": "WARNING"})
        return best
    return None, False
