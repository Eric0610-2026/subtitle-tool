#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
转写模块：音频提取 + Whisper 模型加载/缓存 + 语音转写
"""
import gc
import logging
import re
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from .config import cfg
from .srt_utils import (
    make_post_mapper, safe_stem, fmt_duration, seconds_to_srt_time,
    SubtitleBlock, sanitize_blocks, parse_srt,
)

logger = logging.getLogger(__name__)

try:
    from faster_whisper import WhisperModel
    _WHISPER_AVAILABLE = True
except ImportError:
    WhisperModel = None  # type: ignore
    _WHISPER_AVAILABLE = False

# 各模型相对速度因子（越大越慢），用于进度条权重估算
_MODEL_SPEED: Dict[str, float] = {k: v for k, v in cfg.whisper.model_speed_factors.__dict__.items()}
# 自适应学习率：每处理一个文件，把实测速度按此比例融合到估算值
_ADAPT_ALPHA = 0.2
# 模型加载锁，防止并发加载同一模型
_model_load_lock = threading.Lock()


class Transcriber:
    """封装音频提取、模型缓存与 Whisper 转写"""

    # 由 pipeline 注入的停止检查回调
    stop_check = None

    def __init__(self):
        self._model_cache: dict = {}
        self._cache_lock = threading.Lock()
        # 子进程注册回调（由 pipeline 注入，用于停止时清理）
        self._register_proc = None
        self._unregister_proc = None

    def attach_proc_handlers(self, register, unregister) -> None:
        self._register_proc = register
        self._unregister_proc = unregister

    def clear_cache(self) -> None:
        """清理模型缓存，尝试释放显存（stop 时调用）"""
        for key in list(self._model_cache.keys()):
            try:
                _, _, model = self._model_cache[key]
                del model
            except Exception:
                pass
        self._model_cache.clear()
        try:
            import torch
            if torch.cuda.is_available():
                gc.collect()
                torch.cuda.empty_cache()
        except Exception:
            pass

    @staticmethod
    def _read_stderr_loop(stream, lines, lock, done):
        try:
            for line in stream:
                with lock:
                    lines.append(line)
        except (ValueError, OSError):
            pass
        finally:
            done.set()

    def extract_audio_with_progress(self, video: Path, ffmpeg: str, duration: float,
                                    audio_path: Path, post: Callable) -> float:
        """ffmpeg 提取音频 + stderr 进度解析，返回最终进度时间"""
        cmd = [ffmpeg, "-y", "-i", str(video), "-vn",
               "-acodec", cfg.whisper.audio_codec, "-ar", str(cfg.whisper.audio_sample_rate),
               "-ac", str(cfg.whisper.audio_channels), str(audio_path)]
        post({"type": "progress", "percent": 0, "stage": "提取音频", "detail": "ffmpeg 提取音频中..."})
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
            creationflags=subprocess.CREATE_NO_WINDOW)
        if self._register_proc:
            self._register_proc(proc)
        stderr_lines: list = []
        stderr_lock = threading.Lock()
        stderr_done = threading.Event()

        reader = threading.Thread(target=self._read_stderr_loop,
                                  args=(proc.stderr, stderr_lines, stderr_lock, stderr_done),
                                  daemon=True)
        reader.start()

        time_pattern = re.compile(r"time=(\d+):(\d+):(\d+)\.(\d+)")
        last_progress_time = 0.0
        last_report = 0.0

        try:
            while True:
                if self.stop_check and self.stop_check():
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=2)
                    raise RuntimeError("用户停止")

                new_lines: list = []
                with stderr_lock:
                    new_lines = list(stderr_lines)
                    stderr_lines.clear()
                for line in new_lines:
                    m = time_pattern.search(line)
                    if m and duration > 0:
                        h, mi, s, ms = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))
                        last_progress_time = h * 3600 + mi * 60 + s + ms / 100
                        pct_inner = (last_progress_time / duration) * 100
                        post({"type": "progress", "percent": pct_inner, "stage": "提取音频",
                              "detail": f"音频提取 {fmt_duration(last_progress_time)}/{fmt_duration(duration)}"})

                now = time.time()
                if now - last_report > 0.5 and duration > 0 and last_progress_time > 0:
                    pct_inner = (last_progress_time / duration) * 100
                    post({"type": "progress", "percent": pct_inner, "stage": "提取音频",
                          "detail": f"音频提取 {fmt_duration(last_progress_time)}/{fmt_duration(duration)}"})
                    last_report = now

                if stderr_done.is_set() and proc.poll() is not None:
                    with stderr_lock:
                        for line in stderr_lines:
                            m = time_pattern.search(line)
                            if m and duration > 0:
                                h, mi, s, ms = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))
                                last_progress_time = h * 3600 + mi * 60 + s + ms / 100
                        stderr_lines.clear()
                    break

                time.sleep(0.05)

            proc.wait()
        finally:
            if self._unregister_proc:
                self._unregister_proc(proc)

        if proc.returncode != 0:
            raise RuntimeError(f"ffmpeg 返回错误码 {proc.returncode}")
        if not audio_path.exists():
            raise RuntimeError("音频文件未生成")
        return last_progress_time

    def load_whisper_model(self, model_dir: Path, device: str, compute_type: str, post: Callable):
        """加载 faster-whisper 模型，优先使用缓存（线程安全）"""
        post({"type": "log", "message": f"加载 Whisper 模型（{device}/{compute_type}）...", "level": "INFO"})
        post({"type": "progress", "percent": 0, "stage": "加载模型", "detail": "加载 faster-whisper 模型..."})

        model_key = f"{model_dir}|{device}|{compute_type}"

        with self._cache_lock:
            cached = self._model_cache.get(model_key)
        if cached:
            model = cached[2]
            post({"type": "progress", "percent": 100, "stage": "加载模型", "detail": "使用已加载的模型缓存"})
            post({"type": "log", "message": "使用已加载的模型缓存", "level": "INFO"})
            return model

        with _model_load_lock:
            with self._cache_lock:
                cached = self._model_cache.get(model_key)
            if cached:
                model = cached[2]
                post({"type": "progress", "percent": 100, "stage": "加载模型", "detail": "使用已加载的模型缓存"})
                post({"type": "log", "message": "使用已加载的模型缓存", "level": "INFO"})
                return model

            if not _WHISPER_AVAILABLE:
                raise RuntimeError("缺少 faster-whisper，请运行: pip install faster-whisper\n"
                                   "如遇外部包管理冲突，可加 --break-system-packages 参数")
            with self._cache_lock:
                self.clear_cache()
            try:
                model = WhisperModel(str(model_dir), device=device, compute_type=compute_type)
                with self._cache_lock:
                    self._model_cache[model_key] = (device, compute_type, model)
                post({"type": "progress", "percent": 100, "stage": "加载模型", "detail": "模型加载完成"})
            except Exception as e:
                if "CUDA" in str(e) or "cuda" in str(e):
                    post({"type": "log", "message": (
                        f"CUDA 加载失败: {e}\n"
                        "可能是 CUDA 环境问题。请尝试切换到 CPU 模式。"
                    ), "level": "WARNING"})
                raise RuntimeError(f"Whisper 模型加载失败: {e}")
        return model

    @staticmethod
    def _estimate_weights(duration: float, model_dir: Path) -> Dict[str, float]:
        """按音频时长 + 模型大小估算各阶段耗时权重，用于动态分配进度条"""
        name = model_dir.stem.lower() if model_dir else "large-v3-turbo"
        model_name = next((k for k in _MODEL_SPEED if k in name), "large-v3-turbo")
        speed = _MODEL_SPEED.get(model_name, 1.5)
        return {
            "extract": duration * 0.15,
            "model": 15.0,
            "transcribe": duration * speed,
        }

    def transcribe_video(self, video: Path, output_dir: Path, opts: dict) -> Tuple[Path, str]:
        post = opts["post"]
        ffmpeg = opts["_ffmpeg"]
        ffprobe = opts.get("_ffprobe")
        model_dir = Path(opts["model_dir"])
        device = opts["device"]
        compute_type = opts["compute_type"]
        language = opts["language"]
        extract_audio = opts.get("extract_audio", True)
        vad_enabled = opts.get("vad_filter", True)
        is_audio = opts.get("_is_audio", False)
        idx = opts.get("_idx", 0)
        total = opts.get("_total", 0)

        # ── 断点续转配置 ──
        checkpoint_enabled = opts.get("checkpoint_enabled",
                                       getattr(cfg.whisper, "checkpoint_enabled", True))
        checkpoint_interval = opts.get("checkpoint_interval",
                                        getattr(cfg.whisper, "checkpoint_interval", 30))
        use_word_timestamps = opts.get("word_timestamps",
                                        getattr(cfg.whisper, "word_timestamps", False))
        partial_srt: Optional[Path] = None
        completed_blocks: List[SubtitleBlock] = []
        resume_offset = 0.0

        with tempfile.TemporaryDirectory(prefix="subtitle_") as temp_dir_str:
            temp_dir = Path(temp_dir_str)
            audio_path = temp_dir / "audio.wav"

            # ── 检查断点文件 ──
            partial_srt = output_dir / f"{safe_stem(video.name)}.partial.srt"
            if checkpoint_enabled and partial_srt.exists():
                try:
                    parsed = parse_srt(partial_srt)
                    if parsed:
                        completed_blocks = parsed
                        resume_offset = parsed[-1].end
                        post({"type": "log",
                              "message": (f"🔁 断点续转：已保存 {len(completed_blocks)} 段，"
                                          f"从 {fmt_duration(resume_offset)} 处恢复"),
                              "level": "INFO"})
                    else:
                        logger.warning("断点文件为空，从头转写")
                except Exception as e:
                    logger.warning("解析断点文件失败: %s，从头转写", e)
                    completed_blocks = []
                    resume_offset = 0.0

            try:
                duration = 0.0
                if extract_audio and not is_audio:
                    duration = self.get_duration(video, ffprobe) if ffprobe else 0
                    weights = self._estimate_weights(duration, model_dir)
                    total_w = sum(weights.values())
                    e_end = weights["extract"] / total_w * 100
                    m_start, m_end = e_end, (weights["extract"] + weights["model"]) / total_w * 100
                    t_start = m_end

                    last_progress_time = self.extract_audio_with_progress(
                        video, ffmpeg, duration, audio_path,
                        make_post_mapper(post, 0, e_end))
                    post({"type": "log", "message": f"音频提取完成（{fmt_duration(last_progress_time)}）", "level": "INFO"})
                    if self.stop_check and self.stop_check():
                        raise RuntimeError("用户停止")
                elif extract_audio and is_audio:
                    post({"type": "log", "message": "音频文件，转换为标准格式...", "level": "INFO"})
                    if ffmpeg:
                        duration = self.extract_audio_with_progress(
                            video, ffmpeg, 300.0, audio_path,
                            make_post_mapper(post, 0, 15))
                    else:
                        audio_path = video
                    post({"type": "log", "message": f"音频转换完成（{fmt_duration(duration)}）", "level": "INFO"})
                    m_start, m_end = 15, 30
                    t_start = 30
                    weights = None
                else:
                    post({"type": "log", "message": "跳过音频提取，直接使用源文件...", "level": "INFO"})
                    audio_path = video
                    m_start, m_end = 0, 15
                    t_start = 15
                    weights = None

                # ── 断点续转：裁剪音频（从已转写位置之后开始）──
                if checkpoint_enabled and resume_offset > 0.0 and ffmpeg:
                    trimmed_path = temp_dir / "audio_trimmed.wav"
                    trim_cmd = [
                        ffmpeg, "-y",
                        "-ss", str(resume_offset),
                        "-i", str(audio_path),
                        "-acodec", cfg.whisper.audio_codec,
                        "-ar", str(cfg.whisper.audio_sample_rate),
                        "-ac", str(cfg.whisper.audio_channels),
                        str(trimmed_path),
                    ]
                    trim_proc = subprocess.Popen(
                        trim_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        text=True, encoding="utf-8", errors="replace",
                        creationflags=subprocess.CREATE_NO_WINDOW)
                    if self._register_proc:
                        self._register_proc(trim_proc)
                    try:
                        trim_proc.wait(timeout=60)
                    finally:
                        if self._unregister_proc:
                            self._unregister_proc(trim_proc)
                    if trim_proc.returncode != 0 or not trimmed_path.exists():
                        logger.warning("音频裁剪失败，回退为从头转写")
                        completed_blocks = []
                        resume_offset = 0.0
                    else:
                        audio_path = trimmed_path
                        post({"type": "log",
                              "message": f"音频已裁剪：从 {fmt_duration(resume_offset)} 开始",
                              "level": "INFO"})

                model = self.load_whisper_model(
                    model_dir, device, compute_type,
                    make_post_mapper(post, m_start, m_end))

                t_post = make_post_mapper(post, t_start, 100)
                t_post({"type": "log", "message": "开始转写...", "level": "INFO"})
                t_post({"type": "progress", "percent": 0, "stage": "转写中", "detail": "正在转写，请稍候..."})
                t_post({"type": "preview_clear"})

                # ── 构建 initial_prompt（断点续转时提供上文语境）──
                init_prompt: Optional[str] = None
                if resume_offset > 0.0 and completed_blocks:
                    context_n = min(5, len(completed_blocks))
                    context_texts = [b.text for b in completed_blocks[-context_n:]]
                    init_prompt = " ".join(context_texts)
                    post({"type": "log",
                          "message": f"传递上文语境（{context_n} 句）给 Whisper",
                          "level": "INFO"})

                segments, info = model.transcribe(
                    str(audio_path), beam_size=cfg.whisper.beam_size, vad_filter=vad_enabled,
                    language=None if language == "auto" else language,
                    initial_prompt=init_prompt,
                    word_timestamps=use_word_timestamps)
                detected_lang = info.language
                t_post({"type": "language", "message": f"语言：{detected_lang}"})

                transcribe_start = time.time()
                source_srt = output_dir / f"{safe_stem(video.name)}.{detected_lang}.srt"
                # ── 从断点 segments 开始累积 ──
                blocks: List[SubtitleBlock] = list(completed_blocks)
                prev_end = resume_offset
                srt_lines = []
                for seg in segments:
                    if self.stop_check and self.stop_check():
                        # 中断前保存 checkpoint
                        if checkpoint_enabled and partial_srt and blocks:
                            try:
                                Transcriber._write_partial_srt(partial_srt, blocks)
                            except OSError as e:
                                logger.warning("停止时断点写入失败: %s", e)
                        raise RuntimeError("用户停止")
                    # ── 偏移时间戳 ──
                    seg_start = seg.start + resume_offset
                    seg_end = seg.end + resume_offset
                    gap = seg_start - prev_end
                    if gap > cfg.whisper.silence_gap_seconds and blocks:
                        blocks[-1] = SubtitleBlock(blocks[-1].index, blocks[-1].start,
                                                   seg_start - 0.3, blocks[-1].text)
                    blocks.append(SubtitleBlock(len(blocks) + 1, seg_start, seg_end, seg.text.strip()))
                    prev_end = seg_end
                    pct_inner = (seg_end / duration) * 100 if duration > 0 else 0
                    seg_time_str = seconds_to_srt_time(seg_start).split(",")[0]
                    time_info = f"{seg_time_str} / {fmt_duration(duration)}" if duration > 0 else seg_time_str
                    t_post({"type": "progress", "percent": min(pct_inner, 100), "stage": "转写中",
                            "detail": f"{len(blocks)} 段 | {time_info} | {detected_lang}",
                            "generated": idx, "total": total})
                    if len(blocks) <= 8 or len(blocks) % 10 == 0:
                        time_range = f"[{seg_time_str}]"
                        t_post({"type": "preview_append", "message": f"{len(blocks):>4} {time_range}  {seg.text.strip()}\n"})
                    # ── 定期写入断点文件 ──
                    if checkpoint_enabled and partial_srt and len(blocks) % checkpoint_interval == 0:
                        try:
                            Transcriber._write_partial_srt(partial_srt, blocks)
                        except OSError as e:
                            logger.warning("断点写入失败: %s", e)
                sanitize_blocks(blocks)
                for block in blocks:
                    srt_lines.append(str(block.index))
                    srt_lines.append(block.timing)
                    srt_lines.append(block.text)
                    srt_lines.append("")
                source_srt.write_text("\n".join(srt_lines), encoding="utf-8")
                # ── 转写完成，清理断点文件 ──
                if checkpoint_enabled and partial_srt and partial_srt.exists():
                    try:
                        partial_srt.unlink(missing_ok=True)
                        logger.debug("断点文件已清理: %s", partial_srt)
                    except OSError:
                        pass
                t_post({"type": "progress", "percent": 100, "stage": "转写完成",
                        "detail": f"转写完成：{len(blocks)} 段字幕（{detected_lang}）"})
                transcribe_elapsed = time.time() - transcribe_start
                if duration > 0 and transcribe_elapsed > 0:
                    observed_speed = transcribe_elapsed / duration
                    name = model_dir.stem.lower() if model_dir else ""
                    model_name = next((k for k in _MODEL_SPEED if k in name), None)
                    if model_name:
                        old = _MODEL_SPEED[model_name]
                        _MODEL_SPEED[model_name] = old * (1 - _ADAPT_ALPHA) + observed_speed * _ADAPT_ALPHA
                        post({"type": "log", "message": f"速度因子调整：{old:.2f} → {_MODEL_SPEED[model_name]:.2f}", "level": "INFO"})
                t_post({"type": "log", "message": f"转写完成：{len(blocks)} 段字幕（{detected_lang}）", "level": "INFO"})
                return source_srt, detected_lang
            except Exception:
                # ── 异常时保存断点 ──
                if checkpoint_enabled and partial_srt:
                    try:
                        Transcriber._write_partial_srt(partial_srt, blocks)
                        logger.info("异常时已保存断点: %s (%d 段)", partial_srt, len(blocks))
                    except UnboundLocalError:
                        pass  # blocks 尚未赋值，无进度可保存
                    except Exception as save_err:
                        logger.warning("保存断点失败: %s", save_err)
                raise

    @staticmethod
    def _write_partial_srt(path: Path, blocks: List[SubtitleBlock]) -> None:
        """原子写入部分 SRT 文件（用于断点续转）"""
        tmp = path.with_suffix(path.suffix + ".tmp")
        lines = []
        for b in blocks:
            lines.append(str(b.index))
            lines.append(b.timing)
            lines.append(b.text)
            lines.append("")
        tmp.write_text("\n".join(lines), encoding="utf-8")
        try:
            tmp.replace(path)
        except OSError:
            shutil.copy2(str(tmp), str(path))
            tmp.unlink(missing_ok=True)

    def get_duration(self, video: Path, ffprobe: str) -> float:
        proc = None
        try:
            cmd = [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(video)]
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    text=True, encoding="utf-8", errors="replace",
                                    creationflags=subprocess.CREATE_NO_WINDOW)
            if self._register_proc:
                self._register_proc(proc)
            try:
                stdout, stderr = proc.communicate(timeout=cfg.translation.ffprobe_timeout)
                if proc.returncode == 0:
                    return float(stdout.strip())
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        except Exception as e:
            logger.warning("获取视频时长失败: %s", e)
        finally:
            if proc is not None and self._unregister_proc:
                self._unregister_proc(proc)
        return 0.0
