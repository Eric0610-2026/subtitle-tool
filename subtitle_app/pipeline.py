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
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from .srt_utils import (
    VIDEO_EXTS, AUDIO_EXTS, SUB_EXTS, safe_stem,
    find_existing_subtitle, find_tool,
    IGNORE_FILE,
)
from .transcriber import Transcriber
from .translation import TranslationStopped
from .translator import translate_stage

logger = logging.getLogger(__name__)


def _is_resume_srt_candidate(f2: Path, stem: str, final_srt: Path) -> bool:
    """断点续翻的源字幕候选：与视频同 stem（或带语言后缀），
    排除备份/译文/断点文件与最终输出本身，避免拿别的视频的字幕续翻"""
    f_stem = f2.stem
    if "bak" in f_stem or "translated" in f_stem or "partial" in f_stem:
        return False
    if f2.resolve() == final_srt.resolve():
        return False
    return f_stem == stem or f_stem.startswith(stem + ".")


class SubtitleWorker:
    def __init__(self):
        self._stop_event = threading.Event()
        self.thread: Optional[threading.Thread] = None
        self._progress_file: Optional[Path] = None
        self._active_procs: List[subprocess.Popen] = []
        self._procs_lock = threading.Lock()
        # 真实完成计数（counter 事件的数据源，文件级并发下文件序号≠完成数）
        self._counters_lock = threading.Lock()
        self._counters = {"transcribed": 0, "translated": 0}
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
        try:
            self._run_impl(jobs, opts)
        except Exception as e:
            # 顶层兜底：逐文件 try 之外的意外异常（如配置字段类型异常）
            # 不能让 worker 线程静默死亡，否则 UI 永远收不到 done/error，
            # 按钮永久卡在"运行中"状态，只能重启应用
            tb = traceback.format_exc()
            logger.error("worker 线程意外异常: %s\n%s", e, tb)
            try:
                opts["post"]({"type": "error", "message": f"处理出错: {e}", "trace": tb})
            except Exception:
                pass

    def _bump_counter(self, kind: str) -> None:
        with self._counters_lock:
            self._counters[kind] += 1

    def _post_counter(self, post: Callable, total: int, cache: int = 0) -> None:
        """广播真实完成计数（旧实现用文件序号当累计值，并行乱序完成时完全失真）"""
        with self._counters_lock:
            snap = dict(self._counters)
        post({"type": "counter",
              "generated": snap["transcribed"], "translated": snap["translated"],
              "total": total, "cache": cache})

    def _make_translated_bumper(self, post: Callable, total: int) -> Callable:
        """translate_only 在单个文件全部输出完成后调用：已翻译 +1 并广播计数"""
        def bump(cache: int = 0) -> None:
            self._bump_counter("translated")
            self._post_counter(post, total, cache)
        return bump

    def _run_impl(self, jobs: List[Path], opts: dict) -> None:
        post = opts["post"]
        total = len(jobs)

        # 同名 stem 冲突：同目录 a.mp4 / a.mkv 会共用 partial.srt、{stem}.srt、
        # {stem}.mkv 输出名，并行内嵌时甚至互相覆盖删除 → 直接报错让用户处理
        conflicts = self._find_stem_conflicts(jobs)
        if conflicts:
            names = "、".join(f"{a} 与 {b}" for a, b in conflicts)
            post({"type": "error",
                  "message": f"检测到同名不同格式的文件（{names}），输出的字幕/MKV 会互相覆盖。"
                             "请将其中一方移出该目录或重命名后重试。"})
            return

        # 计数器归零（worker 实例跨批次复用）
        with self._counters_lock:
            self._counters = {"transcribed": 0, "translated": 0}
        # 翻译完成回调：translate_only 在文件输出落盘后调用，更新真实计数
        opts["_bump_translated"] = self._make_translated_bumper(post, total)

        # ── 统一两阶段调度：阶段 1 全部转写 → 释放显存 → 阶段 2 翻译+嵌入 ──
        # 串行（concurrency=1）只是文件级并发为 1 的特例，不再有独立的逐文件路径：
        # 旧串行路径每个文件都要「杀 llama → 加载 Whisper → 释放 → 再拉起 llama」，
        # N 个文件重载 2N 次模型；两阶段调度下两个模型各只加载一次。
        self._run_staged(jobs, opts)

    @staticmethod
    def _find_stem_conflicts(jobs: List[Path]) -> List[Tuple[str, str]]:
        """同目录、同 safe_stem 的视频/音频文件 → 输出名（partial/srt/mkv）必然冲突。

        字幕文件是源文件（a.mp4 + a.srt 是正常用法），不参与冲突判定。
        stem 按小写比较：Windows 文件名大小写不敏感，a.mp4 与 A.mkv 的
        输出 {stem}.srt / {stem}.mkv 是同一个文件。
        """
        seen: dict = {}
        conflicts: List[Tuple[str, str]] = []
        for j in jobs:
            if j.suffix.lower() in SUB_EXTS:
                continue
            key = (j.parent, safe_stem(j.name).lower())
            if key in seen and seen[key].lower() != j.name.lower():
                conflicts.append((seen[key], j.name))
            else:
                seen[key] = j.name
        return conflicts

    def _run_staged(self, jobs: List[Path], opts: dict) -> None:
        """两阶段批量调度。

        阶段 1 全部转写（GPU 只驻留 Whisper）→ 释放显存 → 阶段 2 全部翻译 + 自动嵌入
        （GPU 只驻留 llama.cpp）。避免两者同时占用显存导致 OOM。

        翻译走本地 llama-server（--parallel 1），阶段 2 顺序执行：
        每个文件翻译+内嵌完再处理下一个，pause_before_embed 逐文件生效。
        """
        post = opts["post"]
        total = len(jobs)
        translate_errors: List[Tuple[str, Exception, str]] = []

        # ── 阶段 1：全部转写 ──
        # 仅当确有文件需要真正转写时才清理本地翻译服务
        #（纯字幕批 / 媒体都已有字幕时不必重启 llama-server）
        if self._batch_needs_transcribe(jobs):
            self._prepare_transcribe_phase(opts, post)
        post({"type": "log", "message": f"阶段 1/2：开始转写 {total} 个文件（GPU 用于语音识别）…", "level": "INFO"})
        results: List[dict] = []
        transcribe_failures: List[str] = []
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
                transcribe_failures.append(item.name)
                post({"type": "log", "message": f"❌ 转写失败（已跳过，继续下一个）: {item.name} — {e}",
                      "level": "ERROR", "trace": tb})
                continue
            if result is not None:
                results.append(result)
        if self.stop_requested:
            try:
                self.transcriber.release_model()
            except Exception:
                pass
            post({"type": "done", "message": "用户已停止处理", "stopped": True})
            return
        if not results:
            try:
                self.transcriber.release_model()
            except Exception:
                pass
            if transcribe_failures:
                post({"type": "error",
                      "message": f"转写出错: 全部 {len(transcribe_failures)} 个文件转写失败（详见日志）"})
            else:
                post({"type": "done", "message": "所有任务处理完成！（没有需要翻译的字幕）"})
            return

        # ── 阶段切换：释放 Whisper 显存，为翻译模型腾位 ──
        try:
            self.transcriber.release_model()
        except Exception as e:
            logger.warning("释放语音识别显存失败: %s", e)
        post({"type": "log", "message": f"阶段 1/2 完成（{len(results)} 个字幕）。正在释放显存并准备翻译模型"
                                        "（首次加载约需数十秒）…", "level": "INFO"})

        # ── 阶段 2：全部翻译 + 自动嵌入（顺序执行，逐文件预览暂停可用）──
        stage_opts = dict(opts)
        for r in results:
            if self.stop_requested:
                break
            try:
                self._translate_stage(r, stage_opts, post)
            except TranslationStopped:
                pass  # 用户停止：文件静默结束，不算失败
            except Exception as e:
                tb = traceback.format_exc()
                name = r["item"].name
                logger.error("翻译任务异常 %s: %s\n%s", name, e, tb)
                translate_errors.append((name, e, tb))
                post({"type": "log", "message": f"❌ 翻译失败（已跳过）: {name} — {e}",
                      "level": "ERROR", "trace": tb})

        if self.stop_requested:
            post({"type": "done", "message": "用户已停止处理", "stopped": True})
            return

        if translate_errors and len(translate_errors) == len(results):
            # 全部翻译失败：按错误处理，不报"完成"
            _, e, tb = translate_errors[0]
            post({"type": "error", "message": f"翻译出错: {e}", "trace": tb})
            return
        fail_parts: List[str] = []
        if transcribe_failures:
            fail_parts.append(f"{len(transcribe_failures)} 个文件转写失败")
        if translate_errors:
            fail_parts.append(f"{len(translate_errors)} 个文件翻译失败")
        if fail_parts:
            post({"type": "done",
                  "message": f"任务处理完成，{'、'.join(fail_parts)}（已跳过，详见日志）"})
        else:
            post({"type": "done", "message": "所有任务处理完成！"})

    @staticmethod
    def _batch_needs_transcribe(jobs: List[Path]) -> bool:
        """批内是否至少有一个「需要真正转写」的媒体文件。

        纯字幕批（或媒体都已有同名外挂字幕）不需要 Whisper，
        不必为此杀掉正在运行的本地翻译服务再重启（模型加载 10~60 秒）。
        """
        for j in jobs:
            ext = j.suffix.lower()
            if ext not in VIDEO_EXTS and ext not in AUDIO_EXTS:
                continue
            if find_existing_subtitle(j) is None:
                return True
        return False

    def _prepare_transcribe_phase(self, opts: dict, post: Callable) -> None:
        """进入转写阶段前的 GPU 清理：停掉本会话拉起的翻译服务；外部服务只提示不强制杀。

        门控用「本次是否请求翻译」——翻译端点已硬绑本机 llama-server，
        不再通过地址字符串判断。
        """
        if not opts.get("translate_enabled", True):
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
            stem = safe_stem(item.name)
            state_files = []
            if output_dir and output_dir.exists():
                for sf in output_dir.glob("*.translate_state.json"):
                    if sf.stem.startswith(stem + "."):
                        state_files.append(sf)
            if item.parent.exists() and item.parent != output_dir:
                for sf in item.parent.glob("*.translate_state.json"):
                    if sf.stem.startswith(stem + ".") and sf not in state_files:
                        state_files.append(sf)
            has_state = len(state_files) > 0
            # 完成标记：视频看内嵌产物 .mkv，音频看翻译产物 {stem}.srt。
            # 字幕文件的输出与输入同名（{stem}.srt 就是输入本身），存在性无法
            # 区分"已完成"，一律不按文件存在跳过，避免断点续翻把待翻译的字幕误跳过。
            # .mkv 输入时内嵌产物是 {stem}_subbed.mkv（muxer 避免自覆盖的命名），
            # 若用 {stem}.mkv 作标记会指向输入文件本身——必然存在 → 误判"已完成"。
            if is_video:
                if item.suffix.lower() == ".mkv":
                    done_marker = output_dir / f"{safe_stem(item.name)}_subbed.mkv"
                else:
                    done_marker = output_dir / f"{safe_stem(item.name)}.mkv"
            else:
                done_marker = final_srt if is_audio else None
            if done_marker is not None and done_marker.exists() and not has_state:
                file_post({"type": "log", "message": f"跳过：{item.name} 已完成", "level": "INFO"})
                file_post({"type": "progress", "percent": 100, "stage": "跳过",
                           "detail": "已完成，跳过", "total": total, "cache": 0})
                # 跳过的文件转写/翻译均视为完成（counter 上报真实完成数）
                self._bump_counter("transcribed")
                self._bump_counter("translated")
                self._post_counter(file_post, total)
                file_post({"type": "current", "message": f"[{idx}/{total}] 跳过：{item.name}"})
                return None
            src_srt_for_retry: Optional[Path] = None
            if has_state:
                file_post({"type": "log", "message": f"发现未完成的翻译状态，准备断点续翻：{item.name}", "level": "INFO"})
                if is_subtitle:
                    # 字幕文件的"原文字幕"就是输入文件本身
                    src_srt_for_retry = item
                else:
                    for f2 in sorted(output_dir.glob("*.srt")):
                        if _is_resume_srt_candidate(f2, stem, final_srt):
                            src_srt_for_retry = f2
                            break
                    if not src_srt_for_retry:
                        for f2 in sorted(item.parent.glob("*.srt")):
                            if _is_resume_srt_candidate(f2, stem, final_srt):
                                src_srt_for_retry = f2
                                break
                if src_srt_for_retry:
                    file_post({"type": "file_mode", "needs_transcribe": False, "idx": idx})
                    file_post({"type": "log", "message": f"恢复翻译：使用已有字幕 {src_srt_for_retry.name}", "level": "INFO"})
                    self._bump_counter("transcribed")
                    self._post_counter(file_post, total)
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

        self._bump_counter("transcribed")
        self._post_counter(file_post, total)
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
