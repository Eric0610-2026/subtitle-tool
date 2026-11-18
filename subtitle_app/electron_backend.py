"""JSON-lines adapter for the Electron desktop application."""

import json
import logging
import os
import sys
import threading
import time
import traceback
from logging.handlers import RotatingFileHandler
from pathlib import Path

from . import local_service, muxer, srt_utils as srt, translation
from .config import cfg
from .handoff import HANDOFF_PORT, decode_handoff_request
from .pipeline import SubtitleWorker
from .translator import _progress_file_lock

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "subtitle_app" / "config.json"
CACHE = "cache/.subtitle_translation_cache.json"
MEDIA_EXTS = sorted((srt.VIDEO_EXTS - set(cfg.app.scan_skip_exts)) | srt.AUDIO_EXTS)
SETTING_FIELDS = {
    "app": {"default_video_dir": "default_video_dir"},
    "whisper": {key: key for key in (
        "model_dir", "language", "extract_audio", "vad_filter", "reuse_auto_lang",
    )},
    "translation": {
        "translation_batch_size": "batch_size",
        **{key: key for key in (
            "target_lang", "name_glossaries", "translation_only", "send_all",
            "pause_before_embed", "backup_max_files",
        )},
    },
}
logger = logging.getLogger(__name__)


def find_tool(name):
    return srt.find_tool(f"{name}.exe", ROOT) or srt.find_tool(name, ROOT)


class DesktopBackend:
    def __init__(self, send):
        self.send = send
        self.worker = SubtitleWorker()
        self.lock = threading.RLock()
        self.jobs = []
        self.batch_paths = set()
        self.task = None
        self.task_kind = None
        self.closing = False
        self.stop_thread = None
        self.pause = None
        self.history_path = ROOT / srt.IGNORE_FILE
        old_history = ROOT / ".subtitle_progress.json"
        if old_history.is_file() and not self.history_path.exists():
            srt.save_json(self.history_path, srt.load_json(old_history))
        self.ignored = {Path(path).resolve() for path in self.history().get("ignored", [])}
        self.settings = {
            key: getattr(getattr(cfg, group), field)
            for group, fields in SETTING_FIELDS.items() for key, field in fields.items()
        }
        self.settings["model_dir"] = self.resolve_model_dir(self.settings["model_dir"])
        self.settings["name_glossaries"] = translation.normalize_name_glossaries(
            self.settings["name_glossaries"])

    def emit(self, event):
        self.send({"event": event})

    def log(self, message, level="INFO", trace=None):
        logger.log(getattr(logging, level, logging.INFO), "%s", message)
        self.emit({"type": "log", "message": message, "level": level, "trace": trace})

    @staticmethod
    def resolve_model_dir(value):
        return str((ROOT / Path(value).expanduser()).resolve()) if value else ""

    def initial_state(self):
        warnings = [f"未找到 {tool}，请检查项目 tools 目录或系统 PATH"
                    for tool in ("ffmpeg", "ffprobe") if not find_tool(tool)]
        if not self.settings["model_dir"] or not (Path(self.settings["model_dir"]) / "model.bin").is_file():
            warnings.append(f"未找到 Whisper 的 model.bin：{self.settings['model_dir']}")
        defaults = srt.load_json(CONFIG.with_name("config.example.json")).get("theme", {})
        themes = {}
        for mode in ("light", "dark"):
            colors = getattr(cfg.theme, mode, None)
            themes[mode] = {
                key: color for key, color in getattr(colors, "__dict__", {}).items()
                if isinstance(color, str) and color != defaults.get(mode, {}).get(key)
            }
        return {
            "settings": self.settings, "jobs": self.queue(), "busy": self.task_kind,
            "warnings": warnings, "handoffPort": HANDOFF_PORT,
            "media": [ext[1:] for ext in MEDIA_EXTS],
            "videos": [ext[1:] for ext in sorted(srt.VIDEO_EXTS)],
            "languages": vars(cfg.translation_lang_names),
            "themeOverrides": themes,
            "window": {key: getattr(cfg.app, "window_" + key)
                       for key in ("width", "height", "min_width", "min_height")},
            "maxLogs": cfg.app.max_log_lines,
        }

    def save_settings(self, values, permanent):
        updated = {**self.settings, **{key: values[key] for key in self.settings
                                      if key != "name_glossaries" and key in values}}
        updated["model_dir"] = self.resolve_model_dir(updated["model_dir"].strip())
        updated["default_video_dir"] = updated["default_video_dir"].strip()
        updated["name_glossaries"] = {
            lang: names for lang, text in values["glossary_texts"].items()
            if (names := translation.parse_name_glossary(text))
        }
        for key, lower, upper in (("translation_batch_size", 10, 5000), ("backup_max_files", 0, 10000)):
            value = updated[key]
            if type(value) is not int or not lower <= value <= upper:
                raise ValueError(f"数值须介于 {lower} 和 {upper} 之间")
        if permanent:
            raw = json.loads(CONFIG.read_text(encoding="utf-8")) if CONFIG.exists() else {}
            saved = dict(updated)
            model_dir = saved["model_dir"]
            if model_dir and Path(model_dir).is_relative_to(ROOT):
                saved["model_dir"] = Path(model_dir).relative_to(ROOT).as_posix()
            for group, fields in SETTING_FIELDS.items():
                raw.setdefault(group, {}).update({field: saved[key] for key, field in fields.items()})
            srt.save_json(CONFIG, raw)
        self.settings = updated
        self.log("设置已永久保存" if permanent else "设置已应用（本次运行有效）")
        return updated

    def queue(self):
        return [{"path": str(path), "name": path.name, "size": srt.fmt_file_size(path),
                 "audio": path.suffix.lower() in srt.AUDIO_EXTS,
                 "ignored": path in self.ignored, "active": path in self.batch_paths}
                for path in self.jobs]

    def publish_queue(self):
        self.emit({"type": "queue", "jobs": self.queue()})

    def add(self, paths, check_done=True, files_only=False):
        known = set(self.jobs)
        done = {Path(path).resolve() for path in self.history()["done"]} if check_done else set()
        counts = {"added": 0, "skipped": 0, "invalid": 0}
        for value in paths:
            path = Path(value)
            if not path.is_absolute() and files_only:
                counts["invalid"] += 1
                continue
            candidates = sorted(path.iterdir()) if path.is_dir() and not files_only else [path]
            valid = False
            for candidate in candidates:
                if candidate.suffix.lower() not in MEDIA_EXTS or not candidate.is_file():
                    continue
                valid = True
                candidate = candidate.resolve()
                if candidate in known or candidate in done:
                    counts["skipped"] += 1
                else:
                    known.add(candidate)
                    self.jobs.append(candidate)
                    counts["added"] += 1
            if not valid:
                counts["invalid"] += 1
        self.publish_queue()
        return counts

    def handoff(self, request):
        paths, error = decode_handoff_request(request.encode("utf-8"))
        if error:
            return {"ok": False, "error": error}
        counts = self.add(paths, files_only=True)
        if counts["invalid"] == len(paths):
            return {"ok": False, "error": "没有可导入的视频或音频文件"}
        self.log(f"下载器导入：新增 {counts['added']} 个，跳过 {counts['skipped']} 个，无效 {counts['invalid']} 个")
        return {"ok": True, **counts}

    def change_queue(self, action, paths):
        selected = {Path(path) for path in paths}
        if action == "remove":
            self.jobs = [p for p in self.jobs if p not in selected or p in self.batch_paths]
        elif action == "ignore":
            selected.intersection_update(self.jobs)
            selected.difference_update(self.batch_paths)
            ignored = self.ignored ^ selected
            with _progress_file_lock:
                history = self.history()
                srt.save_json(self.history_path, {**history, "ignored": sorted(map(str, ignored))})
            self.ignored = ignored
        elif action == "reorder":
            order = list(map(Path, paths))
            if len(order) == len(self.jobs) and set(order) == set(self.jobs):
                self.jobs = order
        self.publish_queue()

    def history(self):
        data = srt.load_json(self.history_path)
        return {"done": [item.get("path", "") if isinstance(item, dict) else item
                         for item in data.get("done", [])],
                "ignored": data.get("ignored", [])}

    def remove_history(self, group, paths):
        self.require_idle()
        if group not in {"done", "ignored"}:
            raise ValueError("记录类型无效")
        data = self.history()
        data[group] = [item for item in data[group] if item not in paths]
        srt.save_json(self.history_path, data)
        self.ignored = {Path(path).resolve() for path in data["ignored"]}
        self.publish_queue()
        return data

    def cache(self, paths=None, clear=False):
        cache = ROOT / CACHE
        entries = translation.shared_cache_snapshot(cache)
        if clear or paths is not None:
            self.require_idle()
            translation.remove_shared_cache_entries(entries if clear else paths, cache)
            entries = translation.shared_cache_snapshot(cache)
        return list(entries.items())

    @staticmethod
    def preview(path, media=False):
        file = Path(path)
        if media:
            stem = srt.safe_stem(file.name)
            for directory in (file.parent / stem, file.parent):
                if not directory.is_dir():
                    continue
                candidates = [directory / f"{stem}.srt", *sorted(directory.iterdir())]
                found = next((p for p in candidates if p.is_file() and p.suffix.lower() == ".srt"
                              and srt.is_source_subtitle_stem(p.stem, stem)), None)
                if found:
                    return DesktopBackend.preview(str(found))
            return {"text": "", "path": None}
        if file.suffix.lower() != ".srt":
            raise ValueError("请选择 SRT 字幕")
        return {"text": srt._read_text_auto(file), "path": str(file.resolve())}

    @staticmethod
    def analyze(text, fallback_rows=(), paired=False, offset=None):
        if offset is not None:
            text = srt.shift_srt_timestamps(text, offset)
        blocks = srt.split_srt_blocks(text)
        return {"text": text, "blocks": blocks, "issues": srt.check_subtitle_quality(
            blocks, {tuple(row) for row in fallback_rows}, paired)}

    def save_preview(self, path, text):
        self.require_idle()
        file = Path(path)
        if file.suffix.lower() != ".srt":
            raise ValueError("请选择 SRT 字幕")
        file.write_text(text, encoding="utf-8")
        self.log(f"已保存字幕：{file.name}")

    def require_idle(self):
        if self.closing or self.task_kind is not None:
            raise RuntimeError("请等待当前任务结束")

    def launch(self, kind, handler, *args):
        self.require_idle()
        self.worker.stop_requested = False
        self.task_kind = kind
        self.task = threading.Thread(target=self.run_task, args=(handler, args), daemon=True)
        self.emit({"type": "busy", "kind": kind})
        self.task.start()

    def run_task(self, handler, args):
        try:
            handler(*args)
        except Exception as error:
            logger.exception("后台任务失败")
            self.emit({"type": "error", "message": str(error), "trace": traceback.format_exc()})
        finally:
            with self.lock:
                stopping = self.stop_thread
                if not stopping:
                    self.finish_task()
            if stopping:
                stopping.join()
                with self.lock:
                    self.finish_task()

    def finish_task(self):
        self.stop_thread = None
        self.clear_pause()
        self.batch_paths.clear()
        self.task_kind = None
        self.publish_queue()
        self.emit({"type": "busy", "kind": None})

    def start_processing(self, translate=True, retry=False):
        jobs = [p for p in self.jobs if p not in self.ignored]
        if not jobs:
            raise ValueError("请先添加待处理的视频或音频")
        opts = {
            **self.settings, "work_dir": str(ROOT), "skip_completed": retry,
            "translate_enabled": translate, "device": cfg.whisper.device,
            "compute_type": cfg.whisper.compute_type, "_is_stopped": lambda: self.worker.stop_requested,
            "_register_proc": self.register_proc, "_unregister_proc": self.worker._unregister_proc,
        }
        self.require_idle()
        self.log(f"{'准备断点重试' if retry else '准备处理'} {len(jobs)} 个文件")
        self.launch("pipeline", self.process, jobs, opts)

    def process(self, jobs, opts):
        seen = set()
        failures = []
        start = time.time()
        terminal = {}
        while jobs:
            overall = srt.OverallProgress(len(jobs), cfg.progress.transcribe_weight)
            overall.start()

            def post(event):
                kind = event["type"]
                pending_pause = self.pause
                if pending_pause and time.monotonic() >= pending_pause[0] + 3600:
                    with self.lock:
                        self.clear_pause()
                if kind in {"done", "error"}:
                    terminal.update(event)
                elif kind == "file_mode":
                    if not event["needs_transcribe"]:
                        overall.set_file_translation_only(event["idx"])
                elif kind == "pause_before_embed":
                    self.request_pause(event)
                else:
                    if kind == "progress" and event.get("idx"):
                        percent = overall.tick(event["idx"], event.get("percent", 0), event.get("stage", ""))
                        remaining, eta = overall.eta()
                        event = {**event, "overall": percent, "remaining": remaining, "eta": eta}
                    self.emit(event)

            terminal = {}
            with self.lock:
                if self.worker.stop_requested:
                    break
                self.batch_paths = set(jobs)
                seen.update(self.batch_paths)
                self.publish_queue()
                self.emit({"type": "batch", "total": len(jobs), "first": len(seen) == len(jobs)})
                self.worker.start(jobs, {**opts, "post": post})
            self.worker.thread.join()
            with self.lock:
                self.clear_pause()
                if self.worker.stop_requested:
                    break
                if terminal.get("type") == "error" or "失败" in terminal.get("message", ""):
                    failures.append(terminal.get("message", "处理失败"))
                    self.log(failures[-1], "ERROR", terminal.get("trace"))
                jobs = [p for p in self.jobs if p not in self.ignored and p not in seen]
                if not jobs:
                    break
        stopped = self.worker.stop_requested
        message = "用户已停止处理" if stopped else "所有批次处理完成"
        if failures:
            message += "，部分任务失败，详见日志"
        self.emit({"type": "done", "message": message, "stopped": stopped,
                   "failed": bool(failures), "elapsed": srt.fmt_duration(time.time() - start),
                   "files": len(seen)})

    def register_proc(self, process):
        self.worker._register_proc(process)
        if self.worker.stop_requested and process.poll() is None:
            process.terminate()

    def request_pause(self, event):
        with self.lock:
            if self.worker.stop_requested:
                event["response"].cancel()
                return
            token = time.monotonic()
            self.pause = (token, event["response"])
            self.emit({key: value for key, value in event.items() if key != "response"} | {"token": token})

    def clear_pause(self):
        if self.pause:
            self.pause = None
            self.emit({"type": "pause_closed"})

    def finish_pause(self, token, action, text=None):
        if not self.pause or self.pause[0] != token or self.worker.stop_requested:
            return
        if time.monotonic() < token + 3600:
            _, response = self.pause
            response.action = "skip" if action == "skip" else "embed"
            response.modified_text = text if action == "embed" else None
            response.event.set()
        self.clear_pause()

    def stop(self):
        self.worker.stop_requested = True
        if self.pause:
            self.pause[1].cancel()
            self.clear_pause()
        if self.task and self.task.is_alive() and self.stop_thread is None:
            self.stop_thread = threading.Thread(target=self.worker._terminate_all_procs, daemon=True)
            self.stop_thread.start()

    def model_states(self):
        name = self.worker.transcriber.loaded_model_name()
        loaded = local_service.is_service_running()
        owned = local_service.is_owned_service_running()
        return {"whisper": {"loaded": name is not None, "name": name},
                "hy-mt2": {"loaded": loaded, "owned": owned, "running":
                           loaded or owned or local_service.is_service_listening()}}

    def operate_model(self, model, action):
        if model not in {"whisper", "hy-mt2"} or action not in {"load", "unload"}:
            raise ValueError("模型操作无效")
        self.launch("model", self.change_model, model, action, self.settings)

    def change_model(self, model, action, settings):
        if model == "whisper":
            wh = self.worker.transcriber
            if action == "load":
                path = Path(settings["model_dir"])
                if not settings["model_dir"] or not (path / "model.bin").is_file():
                    raise RuntimeError(f"未找到 Whisper 的 model.bin：{path}")
                wh.load_whisper_model(path, cfg.whisper.device, cfg.whisper.compute_type, self.emit)
            else:
                wh.release_model()
            self.log("Whisper 已加载" if action == "load" else "Whisper 已卸下")
        else:
            if action == "load":
                ok, detail, _ = local_service.ensure_running(
                    stop_check=lambda: self.worker.stop_requested,
                    on_progress=lambda sec: self.emit({
                        "type": "model_progress", "message": f"等待 Hy-MT2 加载，已用 {sec} 秒",
                    }))
            else:
                ok, detail = local_service.shutdown_service()
            self.log(detail or "Hy-MT2 已就绪", "INFO" if ok else "ERROR")
        self.emit({"type": "models", "states": self.model_states()})

    @staticmethod
    def match(path):
        file = Path(path)
        if file.suffix.lower() == ".srt":
            match = srt.match_video_for_subtitle(file, file.parent)
        else:
            match = srt.find_existing_subtitle(file)
        return str(match) if match and match.is_file() else None

    def start_media(self, kind, entries, convert=False):
        ffmpeg = find_tool("ffmpeg")
        if not ffmpeg:
            raise RuntimeError("未找到 ffmpeg")
        if kind not in {"embed", "extract"} or not entries:
            raise ValueError("请先添加文件")
        if kind == "embed":
            records = [(Path(video).resolve(), Path(sub).resolve()) for video, sub in entries]
        else:
            records = [(Path(video).resolve(), None) for video in entries]
        for video, subtitle in records:
            if not video.is_file() or video.suffix.lower() not in srt.VIDEO_EXTS:
                raise ValueError(f"视频文件无效：{video}")
            if subtitle is not None and (not subtitle.is_file() or subtitle.suffix.lower() != ".srt"):
                raise ValueError(f"SRT 文件无效：{subtitle}")
        self.launch(kind, self.process_media, kind, records, ffmpeg, convert)

    def process_media(self, kind, records, ffmpeg, convert):
        success = 0
        self.emit({"type": "batch", "total": len(records), "first": True})
        for index, (video, subtitle) in enumerate(records, 1):
            if self.worker.stop_requested:
                break
            self.log(f"[{index}/{len(records)}] {video.name}")
            args = (ffmpeg, self.emit, self.register_proc, self.worker._unregister_proc)
            if kind == "embed":
                result, trusted = muxer.embed_subtitles_to_video(video, subtitle, *args)
                if result and result.is_file() and trusted:
                    success += 1
                    self.emit({"type": "output_path", "path": str(result)})
                    self.delete_media_sources(result, video, subtitle)
                elif not self.worker.stop_requested:
                    self.log(f"嵌入失败或时长未通过验证，已保留源文件：{video.name}", "WARNING")
            else:
                result, detail = muxer.extract_embedded_subtitle(video, *args)
                if result and result.is_file():
                    success += 1
                    self.emit({"type": "output_path", "path": str(result)})
                    self.log(detail)
                    if convert and not self.worker.stop_requested:
                        mp4, trusted = muxer.convert_to_mp4(video, *args)
                        if mp4 and mp4.is_file():
                            self.emit({"type": "output_path", "path": str(mp4)})
                            if trusted:
                                self.delete_media_sources(mp4, video)
                        if not trusted:
                            self.log(f"转换失败或时长未通过验证，已保留源文件：{video.name}", "WARNING")
                elif not self.worker.stop_requested:
                    self.log(f"提取失败：{video.name} · {detail}", "WARNING")
            self.emit({"type": "media_progress", "index": index, "total": len(records)})
        self.emit({"type": "done", "stopped": self.worker.stop_requested, "failed": success != len(records),
                   "message": f"{'嵌入' if kind == 'embed' else '提取'}完成 {success}/{len(records)} 个"})

    def delete_media_sources(self, result, *sources):
        with self.lock:
            for source in sources:
                if not self.worker.stop_requested and source.resolve() != result.resolve():
                    try:
                        source.unlink()
                        self.log(f"已删除源文件：{source.name}")
                    except OSError as error:
                        self.log(f"删除源文件失败：{error}", "WARNING")

    def close(self):
        with self.lock:
            self.closing = True
            self.stop()
        if self.task:
            self.task.join()
        if self.worker.transcriber.is_loaded():
            self.worker.transcriber.release_model()
        ok, detail = local_service.shutdown_service()
        if not ok:
            self.log(f"退出时关闭 Hy-MT2 失败：{detail}", "WARNING")


def main():
    # Keep the RPC pipes separate from native libraries and media subprocesses.
    requests = os.fdopen(os.dup(sys.stdin.fileno()), "r", encoding="utf-8")
    wire = os.fdopen(os.dup(sys.stdout.fileno()), "w", encoding="utf-8", newline="\n")
    with open(os.devnull, "r") as null_input:
        os.dup2(null_input.fileno(), sys.stdin.fileno())
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    write_lock = threading.Lock()

    def send(message):
        with write_lock:
            wire.write(json.dumps(message, ensure_ascii=False) + "\n")
            wire.flush()

    log_path = ROOT / "logs" / "subtitle_tool.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        handlers=[RotatingFileHandler(log_path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")],
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    backend = DesktopBackend(send)
    commands = {name: getattr(backend, name) for name in (
        "initial_state", "save_settings", "add", "change_queue", "handoff", "history", "remove_history",
        "cache", "preview", "analyze", "save_preview", "start_processing", "finish_pause", "stop",
        "model_states", "operate_model", "match", "start_media",
    )}
    try:
        for line in requests:
            request = {}
            try:
                request = json.loads(line)
                command = request["command"]
                if command == "shutdown":
                    break
                with backend.lock:
                    result = commands[command](*request.get("args", []))
                send({"id": request["id"], "result": result})
            except BrokenPipeError:
                break
            except Exception as error:
                logger.exception("Electron 请求失败")
                send({"id": request.get("id"), "error": str(error)})
    finally:
        try:
            backend.close()
        finally:
            requests.close()
            wire.close()


if __name__ == "__main__":
    main()
