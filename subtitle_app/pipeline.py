#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
工作者线程：转写（faster-whisper）+ 翻译管道路由

本模块编排整体流程（串行 / 并行流水线），转写、翻译、内嵌分别委托
transcriber / translator / muxer 模块完成。
"""
import logging
import queue
import subprocess
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from .config import cfg
from .srt_utils import (
    VIDEO_EXTS, AUDIO_EXTS, SUB_EXTS, safe_stem,
    find_existing_subtitle, match_video_for_subtitle, find_tool,
    IGNORE_FILE,
)
from .transcriber import Transcriber
from .translator import translate_stage

logger = logging.getLogger(__name__)

_STREAM_END = object()  # pipeline 队列结束标记


class SubtitleWorker:
    def __init__(self):
        self._stop_event = threading.Event()
        self.thread: Optional[threading.Thread] = None
        self._progress_file: Optional[Path] = None
        self._active_procs: List[subprocess.Popen] = []
        self._procs_lock = threading.Lock()
        self.transcriber = Transcriber()

    @property
    def stop_requested(self) -> bool:
        return self._stop_event.is_set()

    @stop_requested.setter
    def stop_requested(self, value: bool):
        if value:
            self._stop_event.set()
        else:
            self._stop_event.clear()

    def _register_proc(self, proc) -> None:
        with self._procs_lock:
            self._active_procs.append(proc)

    def _unregister_proc(self, proc) -> None:
        with self._procs_lock:
            try:
                self._active_procs.remove(proc)
            except ValueError:
                pass

    def _terminate_all_procs(self) -> None:
        with self._procs_lock:
            procs = list(self._active_procs)
        for proc in procs:
            try:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=2)
            except Exception as e:
                logger.warning("终止子进程失败: %s", e)

    def _idx_post(self, post: Callable, idx: int, total: int) -> Callable:
        """包装 post，为 progress 事件补上当前文件序号 idx（供总进度计算）"""
        def wrapped(e):
            if isinstance(e, dict) and e.get("type") == "progress":
                e = {**e, "idx": idx, "total": total}
            post(e)
        return wrapped

    def start(self, jobs: List[Path], opts: dict) -> None:
        self.stop_requested = False
        self._progress_file = Path(opts["work_dir"]) / IGNORE_FILE
        self.transcriber.attach_proc_handlers(self._register_proc, self._unregister_proc)
        self.transcriber.stop_check = lambda: self.stop_requested
        self.thread = threading.Thread(target=self._run, args=(jobs, opts), daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_requested = True
        self._terminate_all_procs()
        self.transcriber.clear_cache()
        # 不阻塞 UI 线程：thread 是 daemon 线程，进程退出时自动清理
        # 用极短 timeout 尝试 join，但不阻塞等待
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=0.5)
            if self.thread.is_alive():
                logger.info("worker 线程正在退出中（daemon 将在进程退出时清理）")

    def _run(self, jobs: List[Path], opts: dict) -> None:
        post = opts["post"]
        total = len(jobs)
        p_depth = opts.get("concurrency", 2)

        # ── 串行模式（concurrency=1）: 不走 pipeline，完全顺序执行 ──
        if p_depth <= 1:
            try:
                for idx, item in enumerate(jobs, 1):
                    if self.stop_requested:
                        break
                    self._process_one(item, idx, total, opts)
                if not self.stop_requested:
                    post({"type": "done", "message": "所有任务处理完成！"})
                else:
                    post({"type": "done", "message": "用户已停止处理"})
            except Exception as e:
                tb = traceback.format_exc()
                logger.error("处理出错: %s\n%s", e, tb)
                post({"type": "error", "message": f"处理出错: {e}", "trace": tb})
            return

        # ── 并行流水线（concurrency>=2）: 转写线程生产 → 翻译消费者消费 ──
        tq: "queue.Queue" = queue.Queue(maxsize=p_depth - 1)
        error_info: List[Tuple[Exception, str]] = []

        def _safe_put(val, timeout=0.5):
            while not self.stop_requested:
                try:
                    tq.put(val, timeout=timeout)
                    return
                except queue.Full:
                    continue
            if val is not None and val is not _STREAM_END:
                logger.warning("停止时队列满，转写结果可能丢失: 文件索引 %s", getattr(val, "idx", "?"))

        def transcribe_worker():
            try:
                for idx, item in enumerate(jobs, 1):
                    if self.stop_requested:
                        break
                    result = self._transcribe_stage(item, idx, total, opts)
                    if result is None:
                        _safe_put(None)
                    else:
                        _safe_put(result)
                _safe_put(_STREAM_END)
            except Exception as e:
                tb = traceback.format_exc()
                logger.error("转写线程出错: %s\n%s", e, tb)
                error_info.append((e, tb))
                _safe_put(_STREAM_END)

        trans_thread = threading.Thread(target=transcribe_worker, daemon=True)
        trans_thread.start()

        translate_workers = max(1, getattr(cfg.translation, "concurrency_translate", 3))
        tpool = ThreadPoolExecutor(max_workers=translate_workers)
        try:
            translate_futures = []
            while True:
                if self.stop_requested:
                    # 立即关闭线程池，不等待正在执行的翻译任务
                    tpool.shutdown(wait=False, cancel_futures=True)
                    break
                # 清理已完成 future，检查异常（防止异常被静默吞没）
                done_futures = [f for f in translate_futures if f.done()]
                translate_futures = [f for f in translate_futures if not f.done()]
                for f in done_futures:
                    try:
                        f.result()
                    except Exception as e:
                        logger.error("翻译任务异常（流水线中捕获）: %s", e)
                try:
                    result = tq.get(timeout=0.5)
                except queue.Empty:
                    if not trans_thread.is_alive() and tq.empty():
                        break
                    continue
                if result is _STREAM_END:
                    break
                if result is None:
                    continue
                future = tpool.submit(self._translate_stage, result, opts, post)
                translate_futures.append(future)
        except Exception as e:
            tb = traceback.format_exc()
            logger.error("翻译阶段出错: %s\n%s", e, tb)
            self.stop_requested = True
            post({"type": "error", "message": f"处理出错: {e}", "trace": tb})
            return
        finally:
            # 正常退出时等待任务完成；停止路径中线程池已 shutdown(wait=False)
            if not self.stop_requested:
                tpool.shutdown(wait=True)

        if error_info:
            e, tb = error_info[0]
            post({"type": "error", "message": f"转写出错: {e}", "trace": tb})
            return

        if self.stop_requested:
            post({"type": "done", "message": "用户已停止处理"})
        else:
            post({"type": "done", "message": "所有任务处理完成！"})

    def _transcribe_stage(self, item: Path, idx: int, total: int, opts: dict) -> Optional[dict]:
        """转写阶段：跳过检查 → 音频提取 → Whisper 转写
        返回结果字典供翻译阶段消费，或 None 表示该文件已跳过"""
        post = opts["post"]
        file_post = self._idx_post(post, idx, total)
        work_dir = Path(opts["work_dir"])
        ffmpeg = find_tool("ffmpeg.exe", work_dir) or find_tool("ffmpeg", work_dir)
        ffprobe = find_tool("ffprobe.exe", work_dir) or find_tool("ffprobe", work_dir)
        language = opts["language"]
        skip_completed = opts.get("skip_completed", False)

        file_post({"type": "transcribe_status", "file": item.name, "idx": idx, "total": total})
        file_post({"type": "current", "message": f"[{idx}/{total}] 处理：{item.name}"})

        is_video = item.suffix.lower() in VIDEO_EXTS
        is_audio = item.suffix.lower() in AUDIO_EXTS
        is_subtitle = item.suffix.lower() in SUB_EXTS
        if not is_video and not is_audio and not is_subtitle:
            raise RuntimeError(f"不支持的文件格式: {item.suffix}")

        if is_video:
            output_dir = item.parent
            if not ffmpeg:
                raise RuntimeError("ffmpeg 未找到，请放在应用目录下")
            if not ffprobe:
                raise RuntimeError("ffprobe 未找到，请放在应用目录下")
        elif is_audio:
            output_dir = item.parent
        else:
            matched_video = match_video_for_subtitle(item, work_dir)
            if matched_video:
                output_dir = matched_video.parent / safe_stem(matched_video.name)
            else:
                output_dir = item.parent
        output_dir.mkdir(parents=True, exist_ok=True)

        final_srt = output_dir / f"{safe_stem(item.name)}.srt"

        if skip_completed:
            state_files = []
            if output_dir and output_dir.exists():
                for sf in output_dir.glob("*.translate_state.json"):
                    state_files.append(sf)
            if item.parent.exists() and item.parent != output_dir:
                for sf in item.parent.glob("*.translate_state.json"):
                    if sf not in state_files:
                        state_files.append(sf)
            has_state = len(state_files) > 0
            mkv_path = output_dir / f"{safe_stem(item.name)}.mkv"
            done_marker = mkv_path if is_video else final_srt
            if done_marker.exists() and not has_state:
                file_post({"type": "log", "message": f"跳过：{item.name} 已完成", "level": "INFO"})
                file_post({"type": "progress", "percent": 100, "stage": "跳过",
                           "detail": "已完成，跳过", "total": total, "cache": 0})
                file_post({"type": "counter", "generated": idx, "translated": idx,
                           "total": total, "cache": 0})
                file_post({"type": "current", "message": f"[{idx}/{total}] 跳过：{item.name}"})
                return None
            src_srt_for_retry: Optional[Path] = None
            if has_state:
                file_post({"type": "log", "message": f"发现未完成的翻译状态，准备断点续翻：{item.name}", "level": "INFO"})
                for f2 in sorted(output_dir.glob("*.srt")):
                    if "bak" not in f2.stem and "translated" not in f2.stem and f2.resolve() != final_srt.resolve():
                        src_srt_for_retry = f2
                        break
                if not src_srt_for_retry:
                    for f2 in sorted(item.parent.glob("*.srt")):
                        if "bak" not in f2.stem and "translated" not in f2.stem and f2.resolve() != final_srt.resolve():
                            src_srt_for_retry = f2
                            break
                if src_srt_for_retry:
                    file_post({"type": "file_mode", "needs_transcribe": False, "idx": idx})
                    file_post({"type": "log", "message": f"恢复翻译：使用已有字幕 {src_srt_for_retry.name}", "level": "INFO"})
                    return {
                        "source_srt": src_srt_for_retry,
                        "detected_lang": language,
                        "item": item,
                        "output_dir": output_dir,
                        "idx": idx,
                        "total": total,
                        "_ffmpeg": ffmpeg,
                    }
                else:
                    file_post({"type": "log", "message": "未找到对应的原文字幕文件，重新处理", "level": "INFO"})
                    if not item.exists():
                        raise RuntimeError(f"视频文件不存在且无法恢复: {item}")
            else:
                if not item.exists():
                    raise RuntimeError(f"文件不存在: {item}")
        else:
            if not item.exists():
                raise RuntimeError(f"文件不存在: {item}")

        source_srt: Optional[Path] = None
        detected_lang = language
        if is_video or is_audio:
            existing = find_existing_subtitle(item)
            if existing:
                file_post({"type": "file_mode", "needs_transcribe": False, "idx": idx})
                file_post({"type": "log", "message": f"发现已有字幕：{existing.name}", "level": "INFO"})
                source_srt = existing
                file_post({"type": "progress", "percent": 10, "stage": "读取字幕",
                           "detail": f"使用已有字幕: {existing.name}", "total": total, "cache": 0})
            else:
                source_srt, detected_lang = self.transcriber.transcribe_video(
                    item, output_dir, {
                        **opts, "_ffmpeg": ffmpeg, "_ffprobe": ffprobe,
                        "_idx": idx, "_total": total, "_is_audio": is_audio,
                        "post": file_post})
        else:
            file_post({"type": "file_mode", "needs_transcribe": False, "idx": idx})
            source_srt = item
            file_post({"type": "progress", "percent": 0, "stage": "读取字幕",
                       "detail": f"读取字幕文件: {item.name}", "total": total, "cache": 0})
        if self.stop_requested:
            return None

        return {
            "source_srt": source_srt,
            "detected_lang": detected_lang,
            "item": item,
            "output_dir": output_dir,
            "idx": idx,
            "total": total,
            "_ffmpeg": ffmpeg,
        }

    def _translate_stage(self, result: dict, opts: dict, post: Callable) -> None:
        """翻译阶段：消费转写结果，执行翻译+整理输出"""
        translate_stage(result, opts, self._idx_post(post, result["idx"], result["total"]))

    def _process_one(self, item: Path, idx: int, total: int, opts: dict) -> None:
        """保留兼容性——供外部调用者或测试使用"""
        result = self._transcribe_stage(item, idx, total, opts)
        if result is not None:
            self._translate_stage(result, opts, opts["post"])
