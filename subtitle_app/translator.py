#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
翻译阶段编排：调用 TranslationClient，组装双语/纯译文，落盘与 MKV 内嵌
（注意：翻译客户端本身位于 translation.py，本模块仅负责 worker 侧流程编排）
"""
import logging
import shutil
import threading
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from .config import cfg
from .srt_utils import (
    safe_stem, parse_srt, sanitize_blocks, write_srt, seconds_to_srt_time, has_chinese, to_simplified,
    load_json, save_json, IGNORE_FILE, analyze_subtitle_file, format_quality_report,
    match_video_for_subtitle,
)
from .translation import TranslationClient
from .muxer import embed_subtitles_to_video

logger = logging.getLogger(__name__)


class PauseResponse:
    """用于工作线程与 UI 线程之间的暂停-确认通信"""
    def __init__(self):
        self.event = threading.Event()
        self.action = "embed"  # "embed" | "skip"
        self.modified_text: Optional[str] = None  # None 表示未修改


# ── 备份目录：统一存到 logs/srt_backup（相对于项目根目录）──
_BACKUP_DIR = Path(__file__).resolve().parent.parent / "logs" / "srt_backup"


def _prune_backups(backup_dir: Path, max_files: int) -> None:
    """保留最近 max_files 份 SRT 备份，超出部分按修改时间删除最旧的。

    防止 logs/srt_backup 无限膨胀；max_files<=0 表示不清理。
    """
    if max_files <= 0:
        return
    try:
        files = sorted(backup_dir.glob("*.srt"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return
    for old in files[max_files:]:
        try:
            old.unlink(missing_ok=True)
        except OSError:
            pass


def translate_stage(result: dict, opts: dict, post: Callable) -> None:
    """翻译阶段入口：消费转写结果，执行翻译+整理输出"""
    post({"type": "translate_status", "file": result["item"].name,
          "idx": result["idx"], "total": result["total"]})
    translate_only(
        result["source_srt"], result["output_dir"], result["item"],
        result["idx"], result["total"],
        {**opts, "_detected_lang": result["detected_lang"],
         "_ffmpeg": result.get("_ffmpeg")},
        post,
    )


def translate_only(source_srt: Path, output_dir: Path, item: Path,
                   idx: int, total: int, opts: dict, post: Callable) -> None:
    """只执行翻译+输出，不转写 — 用于断点续翻"""
    work_dir = Path(opts["work_dir"])
    translate_enabled = opts.get("translate_enabled", True)
    api_url = opts.get("api_url", "")
    api_key = opts.get("api_key", "")
    translation_model = opts.get("translation_model", "") or ""
    translation_only = opts.get("translation_only", False)
    language = opts["language"]
    detected_lang = opts.get("_detected_lang", language)
    ffmpeg = opts.get("_ffmpeg")
    is_stopped = opts.get("_is_stopped")

    if is_stopped and is_stopped():
        return

    post({"type": "log", "message": f"解析字幕: {source_srt.name}", "level": "INFO"})
    blocks = parse_srt(source_srt)
    sanitize_blocks(blocks)  # 过滤空文本条目
    post({"type": "counter", "generated": idx, "translated": 0, "total": total})

    if is_stopped and is_stopped():
        return

    translated_srt: Optional[Path] = None
    if translate_enabled and api_url and api_key:
        post({"type": "log", "message": f"开始翻译（{len(blocks)} 条字幕）...", "level": "INFO"})
        cache_path = work_dir / "cache" / ".subtitle_translation_cache.json"
        state_path = source_srt.with_name(source_srt.stem + ".translate_state.json")
        if state_path.exists():
            done = load_json(state_path, {}).get("done", {})
            post({"type": "log", "message": f"断点续翻：已翻译 {len(done)} 句，继续翻译剩余 {len(blocks) - len(done)} 句", "level": "INFO"})
        else:
            state_path = None

        already_translated_idx = set()
        need_translate_idx = []
        for i, block in enumerate(blocks):
            if has_chinese(block.text, detected_lang):
                already_translated_idx.add(i)
            else:
                need_translate_idx.append(i)

        if already_translated_idx:
            post({"type": "log", "message": f"检测到 {len(already_translated_idx)} 条已有中文翻译，跳过翻译", "level": "INFO"})

        trans_concurrency = getattr(cfg.translation, "concurrency_translate", 3)
        send_all = opts.get("send_all", False)
        client = TranslationClient(api_url, api_key, translation_model, cache_path, post,
                                 batch_size=opts.get("translation_batch_size", cfg.translation.batch_size),
                                 target_lang=opts.get("target_lang", "zh"),
                                 send_all=send_all)
        try:
            if need_translate_idx:
                need_blocks = [blocks[i] for i in need_translate_idx]
                is_bilingual = not translation_only
                need_texts = client.translate_blocks(need_blocks, detected_lang,
                                                     is_bilingual, state_path,
                                                     translation_concurrency=trans_concurrency)
                zh_texts = [""] * len(blocks)
                for j, i in enumerate(need_translate_idx):
                    zh_texts[i] = need_texts[j]
                for i in already_translated_idx:
                    zh_texts[i] = blocks[i].text
            else:
                zh_texts = [b.text for b in blocks]
        except RuntimeError as e:
            post({"type": "error", "message": f"断点续翻失败: {e}", "trace": ""})
            raise

        is_chinese_source = detected_lang and (detected_lang.startswith("zh") or language == "zh")
        if translation_only:
            final_texts = zh_texts
        elif not is_chinese_source:
            final_texts = []
            missing_zh = 0
            for block, zh in zip(blocks, zh_texts):
                z = (zh or "").strip()
                src = block.text.strip()
                if z and z != src:
                    # 正常双语
                    final_texts.append(f"{block.text}\n{z}")
                elif z and z == src:
                    # 译文与原文相同（极短语气词等）——仍写两行，避免看起来像「没翻」
                    final_texts.append(f"{block.text}\n{z}")
                elif z:
                    final_texts.append(f"{block.text}\n{z}")
                else:
                    # 仍无译文：保留原文，并计数
                    missing_zh += 1
                    final_texts.append(block.text)
            if missing_zh:
                post({
                    "type": "log",
                    "message": f"⚠ 仍有 {missing_zh}/{len(blocks)} 条字幕无有效译文（已保留原文）",
                    "level": "WARNING",
                })
        else:
            final_texts = zh_texts

        translated_srt = source_srt.with_name(f"{safe_stem(item.name)}.translated.tmp.srt")
        write_srt(translated_srt, blocks, final_texts)

        if is_chinese_source:
            post({"type": "log", "message": "检测到中文源，转换为简体中文...", "level": "INFO"})
            simplified_texts = [to_simplified(t) for t in final_texts]
            write_srt(translated_srt, blocks, simplified_texts)
            final_texts = simplified_texts

        post({"type": "counter", "generated": idx, "translated": idx,
              "total": total, "cache": client.get_cache_size()})
        preview_lines = [f"{i:>4}  {t}" for i, t in enumerate(final_texts, 1)]
        post({"type": "preview", "message": "\n".join(preview_lines)})

        if state_path and state_path.exists():
            try:
                state_path.unlink()
                post({"type": "log", "message": "翻译状态文件已清除", "level": "INFO"})
            except OSError as e:
                logger.warning("删除翻译状态文件失败: %s", e)
    else:
        final_texts = [block.text for block in blocks]
        preview_lines = [f"{i:>4}  {t}" for i, t in enumerate(final_texts, 1)]
        post({"type": "preview", "message": "\n".join(preview_lines)})
        # 即使不翻译，也生成临时字幕文件用于嵌入 MKV
        post({"type": "log", "message": "AI 翻译已关闭，使用原文字幕嵌入", "level": "INFO"})
        translated_srt = source_srt.with_name(f"{safe_stem(item.name)}.translated.tmp.srt")
        write_srt(translated_srt, blocks, final_texts)

    if is_stopped and is_stopped():
        return

    item_stem = safe_stem(item.name)
    is_video = item.suffix.lower() in set(cfg.srt.video_exts)

    # ── 将原文字幕统一备份到 srt_backup 文件夹 ──
    if source_srt and source_srt.exists():
        backup_dir = _BACKUP_DIR
        backup_dir.mkdir(parents=True, exist_ok=True)
        bak_dest = backup_dir / source_srt.name
        if bak_dest.exists():
            stem = source_srt.stem
            suffix = source_srt.suffix
            bak_dest = backup_dir / f"{stem}_{datetime.now().strftime('%Y%m%d_%H%M%S')}{suffix}"
        try:
            shutil.copy2(str(source_srt), str(bak_dest))
            post({"type": "log", "message": f"原文字幕已备份至 logs/srt_backup/{bak_dest.name}", "level": "INFO"})
        except OSError as e:
            logger.warning("备份原文字幕失败: %s", e)
        _prune_backups(backup_dir, getattr(cfg.translation, "backup_max_files", 50))

    # ── 将翻译后的字幕也统一备份到 srt_backup 文件夹 ──
    # 策略：始终保留 srt_backup 中的副本；工作区临时 SRT 仍可按原规则清理
    translated_backup_path: Optional[Path] = None
    if translated_srt and translated_srt.exists():
        backup_dir = _BACKUP_DIR
        backup_dir.mkdir(parents=True, exist_ok=True)
        # 用有意义的文件名区分原文和译文：视频名.translated.srt
        bak_name = f"{item_stem}.translated.srt"
        bak_dest = backup_dir / bak_name
        if bak_dest.exists():
            bak_dest = backup_dir / f"{item_stem}.translated_{datetime.now().strftime('%Y%m%d_%H%M%S')}.srt"
        try:
            shutil.copy2(str(translated_srt), str(bak_dest))
            translated_backup_path = bak_dest
            post({"type": "log", "message": f"翻译字幕已备份至 logs/srt_backup/{bak_dest.name}", "level": "INFO"})
        except OSError as e:
            logger.warning("备份翻译字幕失败: %s", e)
        _prune_backups(backup_dir, getattr(cfg.translation, "backup_max_files", 50))

    # ── 嵌入前质量检查（计数+样例；不阻断自动嵌入）──
    srt_for_quality = None
    if translated_srt and translated_srt.exists():
        srt_for_quality = translated_srt
    elif source_srt and source_srt.exists():
        srt_for_quality = source_srt
    if srt_for_quality is not None:
        report = analyze_subtitle_file(srt_for_quality)
        if report is not None:
            if translated_backup_path is not None:
                report["backup_path"] = str(translated_backup_path.resolve())
            post({"type": "quality_report", **report})
            for line in format_quality_report(report):
                level = "WARNING" if report.get("total_issues") else "INFO"
                post({"type": "log", "message": line, "level": level})

    # ── 嵌入前暂停，供用户预览/编辑 ──
    pause_resp: Optional[PauseResponse] = None
    if is_video and ffmpeg and opts.get("pause_before_embed", False):
        srt_for_embed_pause = translated_srt if (translated_srt and translated_srt.exists()) else None
        if srt_for_embed_pause:
            # 读取翻译后的字幕全文用于预览
            pause_text = "\n\n".join(
                f"{b.index}\n{seconds_to_srt_time(b.start)} --> {seconds_to_srt_time(b.end)}\n{t}"
                for b, t in zip(blocks, final_texts)
            )
            pause_resp = PauseResponse()
            post({
                "type": "pause_before_embed",
                "text": pause_text,
                "file_name": item.name,
                "response": pause_resp,
            })
            # 等待用户确认（最长 1 小时）
            if not pause_resp.event.wait(timeout=3600):
                post({"type": "log", "message": "等待用户确认超时，自动继续嵌入", "level": "WARNING"})
            if pause_resp.action == "skip":
                post({"type": "log", "message": "用户选择跳过嵌入，仅保留外挂字幕", "level": "INFO"})
                # pause_resp.action 保持 "skip"，下游条件会正确处理
            elif pause_resp.modified_text is not None:
                # 用户修改了字幕，写入临时文件用于嵌入
                modified_srt = translated_srt.with_name(f"{safe_stem(item.name)}.pause_modified.srt")
                modified_srt.write_text(pause_resp.modified_text, encoding="utf-8")
                translated_srt = modified_srt
                post({"type": "log", "message": "使用修改后的字幕嵌入", "level": "INFO"})

    # ── 优先尝试内嵌字幕到 MKV ──
    # 视频文件：直接内嵌自身；字幕文件：找同名视频（找不到则只输出外挂字幕）
    mkv_ok = False
    if is_video and ffmpeg and pause_resp is not None and pause_resp.action == "skip":
        pass  # 用户选择跳过嵌入
    elif is_video and ffmpeg:
        video_path = item if item.exists() else (output_dir / item.name)
        srt_for_embed = translated_srt if (translated_srt and translated_srt.exists()) else None
        if srt_for_embed:
            mkv_path, mkv_trusted = embed_subtitles_to_video(
                video_path, srt_for_embed, ffmpeg, post,
                register_proc=opts.get("_register_proc"),
                unregister_proc=opts.get("_unregister_proc"))
            if mkv_path and mkv_path.exists():
                if mkv_trusted:
                    mkv_ok = True
                    post({"type": "output_path", "path": str(mkv_path.resolve())})
                    post({"type": "log", "message": f"✓ 内嵌字幕 MKV 完成: {mkv_path.name}", "level": "INFO"})
                    # 清理临时文件
                    _cleanup_files(post, [(translated_srt, "翻译临时字幕"), (source_srt, "原文字幕")])
                    if video_path.exists():
                        _safe_unlink(video_path, post, "原视频")
                else:
                    post({"type": "log", "message": "⚠️ 内嵌异常，已保留原文件（时长验证未通过，回退外挂字幕）", "level": "ERROR"})
                    try:
                        mkv_path.unlink(missing_ok=True)
                    except OSError:
                        pass
            # 清理 pause_modified 临时文件
            if pause_resp and pause_resp.modified_text is not None:
                modified_srt = translated_srt if translated_srt and translated_srt.exists() else None
                if modified_srt and modified_srt.name.endswith(".pause_modified.srt"):
                    try:
                        modified_srt.unlink(missing_ok=True)
                    except OSError:
                        pass
    elif not is_video and ffmpeg:
        # 已有字幕翻译：尝试匹配同名视频做内嵌
        matched_video = match_video_for_subtitle(item, work_dir)
        srt_for_embed = translated_srt if (translated_srt and translated_srt.exists()) else None
        if matched_video and srt_for_embed:
            mkv_path, mkv_trusted = embed_subtitles_to_video(
                matched_video, srt_for_embed, ffmpeg, post,
                register_proc=opts.get("_register_proc"),
                unregister_proc=opts.get("_unregister_proc"))
            if mkv_path and mkv_path.exists() and mkv_trusted:
                mkv_ok = True
                post({"type": "output_path", "path": str(mkv_path.resolve())})
                post({"type": "log", "message": f"✓ 内嵌字幕 MKV 完成: {mkv_path.name}", "level": "INFO"})
                # 删除原视频与翻译后的字幕（两者均已备份/可重建）
                if matched_video.exists():
                    _safe_unlink(matched_video, post, "原视频")
                if source_srt and source_srt.exists():
                    _safe_unlink(source_srt, post, "翻译字幕")
                _cleanup_files(post, [(translated_srt, "翻译临时字幕")])
    # ── 内嵌失败或非视频 → 整理输出外挂字幕 ──
    if not mkv_ok:
        post({"type": "progress", "percent": 100, "stage": "组织输出", "idx": idx, "total": total})
        post({"type": "log", "message": "正在整理输出文件...", "level": "INFO"})
        # 视频/音频/字幕统一：外挂字幕直接输出到文件所在目录（不再建「视频名/」子目录）
        out_dir = output_dir
        out_dir.mkdir(parents=True, exist_ok=True)
        final_srt = out_dir / f"{item_stem}.srt"


        if translated_srt and translated_srt.exists():
            shutil.copy2(str(translated_srt), str(final_srt))
            translated_srt.unlink(missing_ok=True)
        else:
            write_srt(final_srt, blocks, final_texts)

        post({"type": "progress", "percent": 100, "stage": "完成",
              "detail": f"字幕保存至: {final_srt}", "idx": idx, "total": total})
        post({"type": "output_path", "path": str(final_srt.resolve())})

    # ── 记录进度 ──
    progress_file = Path(opts["work_dir"]) / IGNORE_FILE
    progress_data = load_json(progress_file, {})
    done_list = progress_data.setdefault("done", [])
    abs_path = str(item.resolve())
    if abs_path not in done_list:
        done_list.append(abs_path)
    # 清理历史里遗留的费用字段（功能已移除）
    progress_data.pop("file_cost", None)
    save_json(progress_file, progress_data)
    post({"type": "language", "message": f"语言：{language}"})


def _safe_unlink(path: Path, post: Callable = None, label: str = "") -> None:
    """安全删除文件，带日志"""
    try:
        path.unlink(missing_ok=True)
        if post and label:
            post({"type": "log", "message": f"{label}已删除: {path.name}", "level": "INFO"})
    except OSError as e:
        logger.warning("删除%s失败: %s", label or path.name, e)
        if post:
            post({"type": "log", "message": f"警告：{label}删除失败: {e}", "level": "WARNING"})


def _cleanup_files(post: Callable, paths: list) -> None:
    """批量安全删除文件"""
    for p, label in paths:
        if p and p.exists():
            _safe_unlink(p, post, label)
