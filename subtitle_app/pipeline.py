#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
工作者线程：转写（faster-whisper）+ 翻译管道路由

本模块编排整体流程（串行 / 并行流水线），转写、翻译、内嵌分别委托
transcriber / translator / muxer 模块完成。
"""
import logging
import subprocess
import threading
import traceback
import weakref
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures.thread import _threads_queues, _worker
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from .config import cfg
from .srt_utils import (
    VIDEO_EXTS, AUDIO_EXTS, SUB_EXTS, safe_stem,
    find_existing_subtitle, find_tool,
    IGNORE_FILE,
)
from .transcriber import Transcriber
from .translator import translate_stage

logger = logging.getLogger(__name__)


class _DaemonThreadPoolExecutor(ThreadPoolExecutor):
    """工作线程为 daemon 的线程池。

    ThreadPoolExecutor 的工作线程默认非 daemon：用户停止后，正在执行的
    翻译请求（最长可达 API 超时 × 重试次数）会阻塞解释器退出，导致
    关闭窗口后进程挂起数分钟。这里仅重写线程创建逻辑并标记 daemon。
    """

    def _adjust_thread_count(self):
        # 与父类实现一致，唯一区别是 t.daemon = True
        if self._idle_semaphore.acquire(timeout=0):
            return

        def weakref_cb(_, q=self._work_queue):
            q.put(None)

        num_threads = len(self._threads)
        if num_threads < self._max_workers:
            thread_name = '%s_%d' % (self._thread_name_prefix or self, num_threads)
            t = threading.Thread(name=thread_name, target=_worker,
                                 args=(weakref.ref(self, weakref_cb),
                                       self._work_queue,
                                       self._initializer,
                                       self._initargs))
            t.daemon = True
            t.start()
            self._threads.add(t)
            _threads_queues[t] = self._work_queue


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
        # 模型持久驻留，不再在此处释放显存
        # 如需手动卸载，请调用 self.transcriber.release_model()
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
                if self.stop_requested:
                    # 用户主动停止时，转写线程抛出的"用户停止"异常不算错误
                    post({"type": "done", "message": "用户已停止处理"})
                    return
                tb = traceback.format_exc()
                logger.error("处理出错: %s\n%s", e, tb)
                post({"type": "error", "message": f"处理出错: {e}", "trace": tb})
            return

        # ── 并行模式（concurrency>=2）: GPU 单模型两阶段批量调度 ──
        # RTX 3060 6GB 显存无法同时驻留 Whisper（~2-3G）与 llama-server（~2.3G）。
        # 原「转写线程 + 翻译线程池同跑」的流水线必然爆显存；这里改为
        # 先全部转写 → 释放 Whisper 显存 → 再统一翻译+自动嵌入，从根上错峰。
        self._run_staged(jobs, opts)

    def _run_staged(self, jobs: List[Path], opts: dict) -> None:
        """并行批量的 GPU 单模型两阶段调度。

        阶段 1 全部转写（GPU 只驻留 Whisper）→ 释放显存 → 阶段 2 全部翻译 + 自动嵌入
        （GPU 只驻留 llama.cpp）。避免两者同时占用显存导致 OOM。
        """
        post = opts["post"]
        total = len(jobs)
        trans_concurrency = max(1, getattr(cfg.translation, "concurrency_translate", 3))
        translate_errors: List[Tuple[Exception, str]] = []

        # ── 阶段 1：全部转写 ──
        self._prepare_transcribe_phase(opts, post)
        post({"type": "log", "message": f"阶段 1/2：开始转写 {total} 个文件（GPU 用于语音识别）…", "level": "INFO"})
        results: List[dict] = []
        for idx, item in enumerate(jobs, 1):
            if self.stop_requested:
                break
            try:
                result = self._transcribe_stage(item, idx, total, opts)
            except Exception as e:
                if self.stop_requested:
                    break
                tb = traceback.format_exc()
                logger.error("转写出错 %s: %s\n%s", item.name, e, tb)
                post({"type": "error", "message": f"转写出错: {e}", "trace": tb})
                return
            if result is not None:
                results.append(result)
        if self.stop_requested:
            try:
                self.transcriber.release_model()
            except Exception:
                pass
            post({"type": "done", "message": "用户已停止处理"})
            return
        if not results:
            try:
                self.transcriber.release_model()
            except Exception:
                pass
            post({"type": "done", "message": "所有任务处理完成！（没有需要翻译的字幕）"})
            return

        # ── 阶段切换：释放 Whisper 显存，为翻译模型腾位 ──
        try:
            self.transcriber.release_model()
        except Exception as e:
            logger.warning("释放语音识别显存失败: %s", e)
        post({"type": "log", "message": f"阶段 1/2 完成（{len(results)} 个字幕）。正在释放显存并准备翻译模型"
                                        "（首次加载约需数十秒）…", "level": "INFO"})

        # ── 阶段 2：全部翻译 + 自动嵌入（阶段批量默认自动嵌入，不逐文件暂停预览）──
        stage_opts = dict(opts)
        if stage_opts.get("pause_before_embed", False):
            stage_opts["pause_before_embed"] = False
            post({"type": "log", "message": "阶段批量模式：自动嵌入字幕，跳过逐文件预览暂停", "level": "INFO"})
        tpool = _DaemonThreadPoolExecutor(max_workers=trans_concurrency)
        futures = []
        try:
            for r in results:
                if self.stop_requested:
                    break
                futures.append(tpool.submit(self._translate_stage, r, stage_opts, post))
            for future in futures:
                try:
                    future.result()
                except Exception as e:
                    tb = traceback.format_exc()
                    logger.error("翻译任务异常: %s\n%s", e, tb)
                    translate_errors.append((e, tb))
        finally:
            tpool.shutdown(wait=True)

        if translate_errors:
            e, tb = translate_errors[0]
            post({"type": "error", "message": f"翻译出错: {e}", "trace": tb})
            return

        if self.stop_requested:
            post({"type": "done", "message": "用户已停止处理"})
        else:
            post({"type": "done", "message": "所有任务处理完成！"})

    def _prepare_transcribe_phase(self, opts: dict, post: Callable) -> None:
        """进入转写阶段前的 GPU 清理：停掉本会话拉起的翻译服务；外部服务只提示不强制杀。"""
        if not str(opts.get("api_url", "")).lower().startswith("http://127.0.0.1:8080"):
            return
        try:
            from .local_service import is_service_running, shutdown_owned
            shutdown_owned()   # 停掉上次翻译遗留的本会话 llama-server，为 Whisper 腾显存
            if is_service_running():
                post({"type": "log",
                      "message": "检测到本地翻译服务仍在运行（非本会话启动）。转写将占用大量显存，"
                                 "若提示显存不足请先手动关闭该服务；翻译阶段会自动复用，无需重启",
                      "level": "WARNING"})
        except Exception as e:
            logger.warning("转写前清理本地翻译服务失败: %s", e)

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
        else:
            # 音频与字幕文件：输出都直接写到文件所在目录（字幕不因同名视频进子目录）
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
            # 串行模式：转写后先释放 Whisper 再进入翻译，避免双模型同时驻留显存
            try:
                self.transcriber.release_model()
            except Exception as e:
                logger.warning("释放语音识别显存失败: %s", e)
            self._translate_stage(result, opts, opts["post"])
