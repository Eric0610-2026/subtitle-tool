#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
翻译阶段编排：调用 TranslationClient，组装双语/纯译文，落盘与 MKV 内嵌
（注意：翻译客户端本身位于 translation.py，本模块仅负责 worker 侧流程编排）
"""
import logging
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from .config import cfg
from .srt_utils import (
    safe_stem, parse_srt, write_srt, has_chinese, to_simplified,
    load_json, save_json,
)
from .translation import TranslationClient
from .muxer import embed_subtitles_to_video

logger = logging.getLogger(__name__)


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
    post({"type": "counter", "generated": idx, "translated": 0, "total": total})

    if is_stopped and is_stopped():
        return

    translated_srt: Optional[Path] = None
    if translate_enabled and api_url and api_key:
        post({"type": "log", "message": f"开始翻译（{len(blocks)} 条字幕）...", "level": "INFO"})
        cache_path = work_dir / ".subtitle_translation_cache.json"
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

        client = TranslationClient(api_url, api_key, translation_model, cache_path, post,
                                 batch_size=opts.get("translation_batch_size", cfg.translation.batch_size),
                                 target_lang=opts.get("target_lang", "zh"))
        cost_info = {}
        try:
            detected_lang = language
            if need_translate_idx:
                need_blocks = [blocks[i] for i in need_translate_idx]
                is_bilingual = not translation_only
                need_texts = client.translate_blocks(need_blocks, detected_lang,
                                                     is_bilingual, state_path)
                cost_info = client.get_cost_info()
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
            for block, zh in zip(blocks, zh_texts):
                if zh.strip() and zh.strip() != block.text.strip():
                    final_texts.append(f"{block.text}\n{zh}")
                else:
                    final_texts.append(block.text)
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
        preview_lines = [f"{i:>4}  {t}" for i, t in enumerate(final_texts[:20], 1)]
        post({"type": "preview", "message": "\n".join(preview_lines)})

        if state_path and state_path.exists():
            try:
                state_path.unlink()
                post({"type": "log", "message": "翻译状态文件已清除", "level": "INFO"})
            except OSError as e:
                logger.warning("删除翻译状态文件失败: %s", e)
    else:
        final_texts = [block.text for block in blocks]
        preview_lines = [f"{i:>4}  {t}" for i, t in enumerate(final_texts[:20], 1)]
        post({"type": "preview", "message": "\n".join(preview_lines)})

    if is_stopped and is_stopped():
        return

    item_stem = safe_stem(item.name)
    is_video = item.suffix.lower() in set(cfg.srt.video_exts)

    # ── 优先尝试内嵌字幕到 MKV ──
    mkv_ok = False
    if is_video and ffmpeg:
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
                    for f in [srt_for_embed, source_srt]:
                        if f and f.exists():
                            try:
                                f.unlink(missing_ok=True)
                            except OSError as e:
                                logger.warning("删除临时 SRT 失败: %s", e)
                    if video_path.exists():
                        try:
                            video_path.unlink()
                            post({"type": "log", "message": "原视频已删除，保留 MKV", "level": "INFO"})
                        except OSError as e:
                            logger.error("删除原视频失败: %s", e)
                            post({"type": "log", "message": f"警告：原视频删除失败: {e}", "level": "WARNING"})
                else:
                    post({"type": "log", "message": "⚠️ 内嵌异常，已保留原文件（时长验证未通过，回退外挂字幕）", "level": "ERROR"})
                    try:
                        mkv_path.unlink(missing_ok=True)
                    except OSError:
                        pass
    # ── 内嵌失败或非视频 → 整理输出外挂字幕 ──
    if not mkv_ok:
        post({"type": "progress", "percent": 100, "stage": "组织输出", "idx": idx, "total": total})
        post({"type": "log", "message": "正在整理输出文件...", "level": "INFO"})
        is_retry = opts.get("skip_completed", False)
        if not is_video:
            out_dir = source_srt.parent
        else:
            base_dir = source_srt.parent if is_retry else output_dir
            out_dir = base_dir / item_stem
            out_dir.mkdir(parents=True, exist_ok=True)
        final_srt = out_dir / f"{item_stem}.srt"

        if source_srt and source_srt.exists() and source_srt.resolve() != final_srt.resolve():
            bak_name = f"{item_stem}_bak.srt"
            bak_path = out_dir / bak_name
            if bak_path.exists():
                bak_path = out_dir / f"{item_stem}_bak_{datetime.now().strftime('%Y%m%d_%H%M%S')}.srt"
            try:
                os.rename(str(source_srt), str(bak_path))
                post({"type": "log", "message": f"原文已备份为: {bak_path.name}", "level": "INFO"})
            except OSError as e:
                logger.warning("备份原文字幕失败: %s", e)

        if translated_srt and translated_srt.exists():
            shutil.copy2(str(translated_srt), str(final_srt))
            translated_srt.unlink(missing_ok=True)
        else:
            write_srt(final_srt, blocks, final_texts)

        post({"type": "progress", "percent": 100, "stage": "完成",
              "detail": f"字幕保存至: {final_srt}", "idx": idx, "total": total})
        post({"type": "output_path", "path": str(final_srt.resolve())})

    # ── 记录进度 ──
    progress_file = Path(opts["work_dir"]) / ".subtitle_progress.json"
    progress_data = load_json(progress_file, {})
    done_list = progress_data.setdefault("done", [])
    abs_path = str(item.resolve())
    if abs_path not in done_list:
        done_list.append(abs_path)
    if cost_info:
        file_cost = progress_data.setdefault("file_cost", {})
        file_cost[abs_path] = cost_info
    save_json(progress_file, progress_data)
    post({"type": "language", "message": f"语言：{language}"})
