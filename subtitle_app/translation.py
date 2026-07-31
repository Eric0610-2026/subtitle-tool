#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
翻译客户端：句子级缓存 + 批量翻译 + 断点续翻 + 403 fallback
"""

import json
import logging
import re
import shutil
import time
import urllib.error
import urllib.request
import concurrent.futures
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from threading import Lock
from typing import Any, Callable, Dict, List, Optional, Tuple

from .config import cfg
from .srt_utils import (
    SubtitleBlock, split_sentences, sentence_cache_key,
    load_json, save_json, is_cjk,
)

logger = logging.getLogger(__name__)

# ── 常量（从 config.json 读取）──

API_TIMEOUT = cfg.translation.api_timeout
API_RETRY_COUNT = cfg.translation.retry_count
API_RETRY_BASE = cfg.translation.retry_base_delay
MAX_CACHE_ENTRIES = cfg.translation.max_cache_entries
PARAGRAPH_GAP = cfg.translation.paragraph_gap_seconds
CONTEXT_WINDOW = cfg.translation.context_window
MAX_RECURSION_DEPTH = getattr(cfg.translation, "max_recursion_depth", 5)

LANG_NAMES = {k: v for k, v in cfg.translation_lang_names.__dict__.items()}

def make_prompt(target_lang: str) -> str:
    lang_name = LANG_NAMES.get(target_lang, "简体中文")
    return (
        f"你是严谨的字幕翻译器。将以下数组中的字幕文本逐条翻译为{lang_name}。"
        "要求：\n"
        "1. 保留原文语义和语气\n"
        f"2. 译文符合{lang_name}表达习惯，自然流畅\n"
        "3. 注意上下文连贯\n"
        "4. 专有名词保留原文\n"
        "5. 返回格式严格为 JSON 数组，保持顺序，每个元素为对应译文\n"
        '示例：["你好", "世界"]'
    )

# ── 自定义异常 ──


class ApiForbiddenError(RuntimeError):
    """API 返回 403 时的专用异常，用来触发 curl fallback"""
    pass


def _normalize_response(resp: dict) -> dict:
    """标准化不同厂商 API 响应为 OpenAI 兼容格式。

    处理常见兼容性问题：
    1. 商汤等：choices/usage 包裹在 data 字段下（非标准 OpenAPI）
    2. 部分厂商 data 是列表直接包含 choices/结果
    3. choices[i].message 是字符串而非 {"content": "..."}
    """
    resp = dict(resp)  # 不修改原始 dict

    # 0) 检测顶层 error 字段并直接返回（让调用方提取错误消息）
    if "error" in resp:
        return resp

    # 1) 当顶层无 choices 但 data 中有时，提升到顶层
    if "choices" not in resp and "data" in resp:
        data = resp["data"]
        if isinstance(data, dict):
            if "choices" in data:
                resp["choices"] = data["choices"]
            if "usage" in data and "usage" not in resp:
                resp["usage"] = data["usage"]
            if "id" in data and "id" not in resp:
                resp["id"] = data["id"]
        elif isinstance(data, list):
            # 部分厂商 data 是列表：看列表元素是否包含 message/choices
            if data and isinstance(data[0], dict):
                first = data[0]
                if "message" in first or "content" in first or "text" in first:
                    # data 本身就是 choices 列表
                    resp["choices"] = data
                elif "choices" in first:
                    resp["choices"] = first["choices"]
    # 2) 标准化 choices[i].message 为对象格式
    for choice in resp.get("choices", []):
        if isinstance(choice, dict):
            msg = choice.get("message")
            if isinstance(msg, str):
                choice["message"] = {"content": msg}
            # 部分厂商直接把内容放在 choice.text 而非 message.content
            if "message" not in choice and "text" in choice:
                choice["message"] = {"content": choice["text"]}
    return resp


def _extract_error(resp: dict) -> str:
    """从 API 响应中提取错误消息"""
    # 标准 OpenAI 错误格式：{"error": {"message": "..."}}
    err = resp.get("error")
    if isinstance(err, dict):
        msg = err.get("message", "") or err.get("msg", "") or str(err)
        if msg:
            return msg
    elif isinstance(err, str) and err:
        return err
    # 部分国产 API 用 code/message 表示错误
    code = resp.get("code")
    msg = resp.get("message", "") or resp.get("msg", "")
    # code 为 0 或省略表示成功
    if code is not None and code != 0 and code != "0" and msg:
        return f"[{code}] {msg}"
    # 部分 API 把错误放在 detail 字段
    detail = resp.get("detail")
    if isinstance(detail, str) and detail:
        return detail
    return ""


# ── TranslationClient ──


class TranslationClient:
    """翻译客户端：句子级缓存 + 批量翻译 + 断点续翻 + 403 fallback"""

    def __init__(self, api_url: str, api_key: str, model: str, cache_path: Path,
                 post_ui: Callable, batch_size: int = None, target_lang: str = "zh",
                 send_all: bool = False):
        if batch_size is None:
            batch_size = cfg.translation.batch_size
        self.batch_size = batch_size
        self.send_all = send_all
        self.api_url = api_url
        self.api_key = api_key
        self.model = model
        self.target_lang = target_lang
        self.system_prompt = make_prompt(target_lang)
        self.cache: Dict[str, str] = load_json(cache_path, {})
        self.cache_path = cache_path
        self.post_ui = post_ui
        self._cache_lock = Lock()


    def translate_blocks(self, blocks: List[SubtitleBlock], source_lang: str,
                         is_bilingual: bool, state_path: Optional[Path] = None,
                         translation_concurrency: int = 3) -> List[str]:
        # Step 0: 按时间间隔划分段落
        para_of_block: List[int] = []
        current_para = 0
        para_of_block.append(current_para)
        for i in range(1, len(blocks)):
            gap = blocks[i].start - blocks[i - 1].end
            if gap > PARAGRAPH_GAP:
                current_para += 1
            para_of_block.append(current_para)

        # Step 1: 展开为句子
        block_sents: List[Tuple[int, List[str]]] = []
        for bidx, block in enumerate(blocks):
            sents = split_sentences(block.text)
            block_sents.append((bidx, sents))
        flat: List[Tuple[int, int, str]] = []
        gsid_to_para: List[int] = []
        for bidx, sents in block_sents:
            para = para_of_block[bidx]
            for sent in sents:
                flat.append((len(flat), bidx, sent))
                gsid_to_para.append(para)
        if not flat:
            return [block.text for block in blocks]

        # Step 2: 缓存命中 + 断点恢复（空串不算已完成，避免永久漏翻）
        sent_trans: Dict[int, str] = {}
        # 构建原文映射（用于断点恢复时的内容校验）
        sent_originals: Dict[str, str] = {str(gsid): sent for gsid, _, sent in flat}
        if state_path and state_path.exists():
            state = load_json(state_path, {})
            done = state.get("done", {})
            saved_originals = state.get("originals", {})
            for entry in flat:
                gsid = entry[0]
                gsid_str = str(gsid)
                zh_done = done.get(gsid_str, "")
                if (
                    isinstance(zh_done, str)
                    and zh_done.strip()
                    and saved_originals.get(gsid_str) == sent_originals.get(gsid_str)
                ):
                    sent_trans[gsid] = zh_done.strip()

        to_translate: List[Tuple[int, str]] = []
        for gsid, bidx, sent in flat:
            if gsid in sent_trans and str(sent_trans[gsid]).strip():
                continue
            key = sentence_cache_key(sent, self.model, True)
            cached = self.cache.get(key)
            if isinstance(cached, str) and cached.strip():
                sent_trans[gsid] = cached.strip()
            else:
                # 空缓存 / 无效缓存：重新翻译
                if key in self.cache and not (isinstance(cached, str) and cached.strip()):
                    with self._cache_lock:
                        self.cache.pop(key, None)
                sent_trans[gsid] = ""
                to_translate.append((gsid, sent))

        # 去重
        text_to_gsid: Dict[str, List[int]] = {}
        for gsid, sent in to_translate:
            text_to_gsid.setdefault(sent, []).append(gsid)
        unique_texts = list(text_to_gsid.keys())
        if not unique_texts:
            # 所有句子都命中缓存/断点状态，直接组装译文
            result_texts = _reassemble_blocks(blocks, flat, sent_trans)
            self.post_ui({
                "type": "progress", "percent": 100,
                "stage": "翻译", "detail": f"翻译完成（全部命中缓存，共 {len(blocks)} 条）",
                "total": len(blocks), "cache": len(self.cache),
            })
            return result_texts

        # Step 3: 批量翻译（多线程并发 API 调用）
        effective_batch_size = len(unique_texts) if self.send_all else self.batch_size
        total_batches = (len(unique_texts) + effective_batch_size - 1) // effective_batch_size
        para_context: Dict[int, List[str]] = {}

        with ThreadPoolExecutor(max_workers=translation_concurrency) as executor:
            batch_futures: List[tuple] = []
            for batch_idx in range(0, len(unique_texts), effective_batch_size):
                batch = unique_texts[batch_idx:batch_idx + effective_batch_size]
                batch_id = batch_idx // self.batch_size + 1
                para_ids = set()
                for text in batch:
                    for gsid in text_to_gsid.get(text, []):
                        if gsid < len(gsid_to_para):
                            para_ids.add(gsid_to_para[gsid])
                main_para = min(para_ids) if para_ids else 0
                ctx_list = para_context.get(main_para, [])
                context_text = ""
                if ctx_list:
                    context_lines = [f"（上文）{ctx}" for ctx in ctx_list[-CONTEXT_WINDOW:]]
                    context_text = "\n".join(context_lines) + "\n"
                self.post_ui({
                    "type": "log", "message": f"提交翻译批次 {batch_id}/{total_batches}（{len(batch)} 句）",
                    "level": "INFO",
                })
                future = executor.submit(self._translate_batch, batch, context_text, 0)
                batch_futures.append((future, batch, text_to_gsid, batch_id, main_para))

            # 按提交顺序处理结果（保证段落上下文连续性）
            completed_count = 0
            for future, batch, t2g, batch_id, main_para in batch_futures:
                # 轮询等待，每隔 15s 发送心跳防止 UI 假死
                while True:
                    try:
                        translations = future.result(timeout=15)
                        break
                    except (concurrent.futures.TimeoutError, TimeoutError):
                        self.post_ui({
                            "type": "progress",
                            "percent": (completed_count / max(total_batches, 1)) * 100,
                            "stage": "翻译",
                            "detail": f"批次 {batch_id}/{total_batches} 仍在翻译中（API 响应较慢，已完成 {completed_count}/{total_batches} 批）",
                            "total": len(blocks), "cache": len(self.cache),
                        })
                try:
                    translations = future.result()
                except RuntimeError:
                    self._save_cache()
                    if state_path:
                        save_json(state_path, {
                            "done": _nonempty_done(sent_trans),
                            "originals": sent_originals,
                            "updated_at": datetime.now().isoformat(),
                        })
                    raise
                applied = _apply_batch_translations(
                    batch, translations, t2g, sent_trans, self.cache, self._cache_lock, self.model)
                for orig_text, zh_text in applied:
                    if not zh_text:
                        continue
                    for gsid in t2g.get(orig_text, []):
                        if gsid < len(gsid_to_para):
                            para = gsid_to_para[gsid]
                            para_context.setdefault(para, []).append(zh_text)
                            para_context[para] = para_context[para][-CONTEXT_WINDOW:]
                if state_path:
                    save_json(state_path, {
                        "done": _nonempty_done(sent_trans),
                        "originals": sent_originals,
                        "updated_at": datetime.now().isoformat(),
                    })
                completed_count += 1
                self.post_ui({
                    "type": "progress",
                    "percent": (completed_count / max(total_batches, 1)) * 100,
                    "stage": "翻译",
                    "detail": f"批次 {batch_id}/{total_batches} 完成，进度 {completed_count}/{total_batches}",
                    "total": len(blocks), "cache": len(self.cache),
                })

        # Step 3b: 漏译句子单条补翻（空 zh / 未回填）
        missing = [(gsid, sent) for gsid, bidx, sent in flat
                   if not str(sent_trans.get(gsid, "")).strip()]
        if missing:
            self.post_ui({
                "type": "log",
                "message": f"检测到 {len(missing)} 句未译成功，开始单条补翻…",
                "level": "WARNING",
            })
            # 按原文去重后逐条补
            miss_map: Dict[str, List[int]] = {}
            for gsid, sent in missing:
                miss_map.setdefault(sent, []).append(gsid)
            for i, (orig, gsids) in enumerate(miss_map.items(), 1):
                try:
                    items = self._translate_batch([orig], "", 0)
                    zh = ""
                    if items:
                        it = items[0]
                        if not it.get("zh") and it.get("en"):
                            it["zh"] = it["en"]
                        zh = str(it.get("zh") or "").strip()
                    if not zh:
                        # 最后兜底：纯文本单句
                        plain = self._call_api_single_plain(orig)
                        if plain:
                            zh = str(plain[0].get("zh") or "").strip()
                    if zh and zh != orig:
                        key = sentence_cache_key(orig, self.model, True)
                        with self._cache_lock:
                            self.cache[key] = zh
                        for gsid in gsids:
                            sent_trans[gsid] = zh
                    elif zh:
                        # 与原文相同也写入，避免反复补翻同一句；双语组装层会处理
                        for gsid in gsids:
                            sent_trans[gsid] = zh
                    else:
                        logger.warning("单条补翻仍无结果: %r", orig[:80])
                except Exception as e:
                    logger.warning("单条补翻失败 %r: %s", orig[:80], e)
                if i % 10 == 0 or i == len(miss_map):
                    self.post_ui({
                        "type": "progress",
                        "percent": 95 + 5 * (i / max(len(miss_map), 1)),
                        "stage": "翻译",
                        "detail": f"补翻进度 {i}/{len(miss_map)}",
                        "total": len(blocks), "cache": len(self.cache),
                    })
            if state_path:
                save_json(state_path, {
                    "done": _nonempty_done(sent_trans),
                    "originals": sent_originals,
                    "updated_at": datetime.now().isoformat(),
                })
            still = sum(1 for gsid, _, _ in flat if not str(sent_trans.get(gsid, "")).strip())
            if still:
                self.post_ui({
                    "type": "log",
                    "message": f"补翻后仍有 {still} 句无译文（将尽量用已有部分组装）",
                    "level": "WARNING",
                })

        # Step 4: 立即告知 UI 翻译完成（不影响后续 I/O）
        self.post_ui({
            "type": "progress", "percent": 100,
            "stage": "翻译", "detail": f"翻译完成（共 {len(blocks)} 条）",
            "total": len(blocks), "cache": len(self.cache),
        })

        # 拼回块（允许部分句成功时仍输出已有译文，不再整块丢弃）
        result_texts = _reassemble_blocks(blocks, flat, sent_trans)

        # 后台 I/O（保存缓存，用户已看到 100%）
        self._save_cache()
        return result_texts

    def _translate_batch(self, texts: List[str], context: str = "", depth: int = 0) -> List[Dict]:
        """批量翻译，带递归深度限制防止栈溢出"""
        prompt_text = json.dumps(texts, ensure_ascii=False)
        if context:
            prompt_text = context + prompt_text
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": prompt_text},
            ],
            "temperature": cfg.translation.temperature,
            "stream": False,
        }
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        try:
            resp_data = self._call_api(payload, headers)
        except ApiForbiddenError:
            try:
                resp_data = self._curl_fallback(payload, headers)
            except Exception as e2:
                raise RuntimeError(f"翻译 API 调用失败（curl fallback 也失败）: {e2}")
        except Exception as e:
            # 修正缩进：正确处理递归拆分，增加深度限制
            if len(texts) > 1 and depth < MAX_RECURSION_DEPTH:
                logger.warning("批量翻译失败（深度 %d），拆分为小批次重试: %s", depth, e)
                mid = len(texts) // 2
                return (self._translate_batch(texts[:mid], context, depth + 1) +
                        self._translate_batch(texts[mid:], context, depth + 1))
            elif depth >= MAX_RECURSION_DEPTH:
                logger.error("翻译递归深度超过限制 %d，返回原文", MAX_RECURSION_DEPTH)
                return [{"id": i + 1, "zh": t} for i, t in enumerate(texts)]
            else:
                # 单句失败，尝试纯文本模式
                logger.warning("单句翻译失败，尝试纯文本模式: %s", e)
                return self._call_api_single_plain(texts[0])

        # 响应解析（独立 try，确保解析失败也触发递归拆分）
        try:
            return self._parse_translation_response(resp_data)
        except Exception as e:
            logger.warning("翻译响应解析失败（深度 %d），拆分为小批次重试: %s", depth, e)
            if len(texts) > 1 and depth < MAX_RECURSION_DEPTH:
                mid = len(texts) // 2
                return (self._translate_batch(texts[:mid], context, depth + 1) +
                        self._translate_batch(texts[mid:], context, depth + 1))
            elif depth >= MAX_RECURSION_DEPTH:
                logger.error("翻译响应解析失败，超过递归深度限制 %d，返回原文", MAX_RECURSION_DEPTH)
                return [{"id": i + 1, "zh": t} for i, t in enumerate(texts)]
            else:
                logger.warning("单句翻译响应解析失败，尝试纯文本模式: %s", e)
                return self._call_api_single_plain(texts[0])

    def _call_api(self, payload: dict, headers: dict) -> dict:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(self.api_url, data=data, headers=headers, method="POST")
        last_err: Optional[Exception] = None
        for attempt in range(1, API_RETRY_COUNT + 1):
            try:
                with urllib.request.urlopen(req, timeout=API_TIMEOUT) as resp:
                    resp_json = json.loads(resp.read().decode("utf-8"))
                    return _normalize_response(resp_json)
            except urllib.error.HTTPError as e:
                if e.code in (401, 402, 403, 407):
                    body = e.read().decode("utf-8", errors="replace")
                    err_detail = body[:200]
                    if e.code == 403:
                        raise ApiForbiddenError(f"API 返回 403: {err_detail}")
                    raise RuntimeError(f"API 认证错误 (HTTP {e.code}): {err_detail}")
                body = e.read().decode("utf-8", errors="replace")
                last_err = RuntimeError(f"HTTP {e.code}: {body[:200]}")
                logger.warning("API HTTP 错误 (尝试 %d/%d): %s", attempt, API_RETRY_COUNT, e.code)
            except urllib.error.URLError as e:
                last_err = RuntimeError(f"网络错误: {e.reason}")
                logger.warning("API 网络错误 (尝试 %d/%d): %s", attempt, API_RETRY_COUNT, e.reason)
            except TimeoutError:
                last_err = RuntimeError("API 请求超时")
                logger.warning("API 超时 (尝试 %d/%d)", attempt, API_RETRY_COUNT)
            except Exception as e:
                last_err = RuntimeError(f"API 请求失败: {e}")
                logger.warning("API 请求异常 (尝试 %d/%d): %s", attempt, API_RETRY_COUNT, e)
            if attempt < API_RETRY_COUNT:
                delay = API_RETRY_BASE * (2 ** (attempt - 1))
                logger.info("等待 %.1f 秒后重试...", delay)
                time.sleep(delay)
        if last_err is None:
            last_err = RuntimeError("API 请求失败（无具体错误）")
        raise last_err

    def _curl_fallback(self, payload: dict, headers: dict) -> dict:
        curl = shutil.which("curl.exe") or shutil.which("curl")
        if not curl:
            raise RuntimeError("curl not found")
        tmp_file = self.cache_path.parent / f".curl_payload_{int(time.time())}_{id(self)}.json"
        save_json(tmp_file, payload)
        try:
            header_args = []
            for k, v in headers.items():
                header_args.extend(["-H", f"{k}: {v}"])
            cmd = [curl, "-s", "-X", "POST", self.api_url, *header_args,
                   "--data-binary", f"@{tmp_file}", "--max-time", str(getattr(cfg.translation, "timeout_curl", 120))]
            result = subprocess_run_safe(cmd, timeout=API_TIMEOUT + 10)
            if result.returncode != 0:
                raise RuntimeError(f"curl failed: {result.stderr[:200]}")
            return _normalize_response(json.loads(result.stdout))
        finally:
            try:
                tmp_file.unlink(missing_ok=True)
            except Exception:
                pass

    def _call_api_single_plain(self, text: str) -> List[Dict]:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": "你是严谨的字幕翻译器。将以下文本翻译为简体中文，只返回译文，不要其他内容。"},
                {"role": "user", "content": text},
            ],
            "temperature": cfg.translation.temperature, "stream": False,
        }
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        try:
            resp_data = self._call_api(payload, headers)
            # 先检测 API 错误
            error_msg = _extract_error(resp_data)
            if error_msg:
                logger.warning("纯文本翻译 API 返回错误: %s", error_msg)
            else:
                choices = resp_data.get("choices")
                if choices and isinstance(choices, list) and len(choices) > 0:
                    content = choices[0].get("message", {}).get("content", "")
                    if content:
                        return [{"id": 1, "zh": content.strip()}]
        except Exception as e:
            logger.warning("纯文本单句翻译也失败: %s，返回原文", e)
        return [{"id": 1, "zh": text}]

    def _parse_translation_response(self, resp_data: dict) -> List[Dict]:
        items = None

        # 先检测 API 错误响应
        error_msg = _extract_error(resp_data)
        if error_msg:
            logger.error("API 返回错误: %s", error_msg)
            raise RuntimeError(f"翻译 API 返回错误: {error_msg}")

        try:
            choices = resp_data.get("choices")
            if choices and isinstance(choices, list) and len(choices) > 0:
                content = choices[0].get("message", {}).get("content", "")
            else:
                content = ""
            if content:
                parsed = _extract_json(content)
                if parsed:
                    if isinstance(parsed, list):
                        items = parsed
                    else:
                        items = parsed.get("items") or parsed.get("translations") or parsed.get("result") or parsed
        except Exception as e:
            logger.warning("解析翻译响应内容失败: %s", e)
        if items is None:
            items = resp_data.get("items") or resp_data.get("translations") or resp_data.get("result") or resp_data.get("data")
        if isinstance(items, list):
            if items and isinstance(items[0], str):
                return [{"id": i + 1, "zh": t} for i, t in enumerate(items)]
            if items and isinstance(items[0], dict):
                result = []
                for item in items:
                    id_val = item.get("id", len(result) + 1)
                    zh_val = item.get("zh") or item.get("text") or item.get("translation") or item.get("en") or ""
                    result.append({"id": id_val, "zh": zh_val})
                return result
        if items is None:
            logger.error("无法解析翻译响应: %s",
                         json.dumps(resp_data, ensure_ascii=False, indent=2)[:1000])
            raise RuntimeError("无法解析翻译响应（请检查 API Key 和模型名称）")
        return items if isinstance(items, list) else []

    def _save_cache(self) -> None:
        with self._cache_lock:
            if len(self.cache) > MAX_CACHE_ENTRIES:
                # FIFO 裁剪：移除最旧条目，保留最新的 MAX_CACHE_ENTRIES//2 条
                excess = len(self.cache) - MAX_CACHE_ENTRIES // 2
                keys_to_remove = list(self.cache.keys())[:excess]
                for k in keys_to_remove:
                    del self.cache[k]
            save_json(self.cache_path, self.cache)

    def get_cache_size(self) -> int:
        return len(self.cache)


# ── 安全的 subprocess.run 包装 ──

def subprocess_run_safe(cmd: List[str], timeout: int) -> Any:
    """安全的 subprocess.run，避免 shell=True"""
    import subprocess
    creationflags = 0
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        creationflags = subprocess.CREATE_NO_WINDOW
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        encoding="utf-8",
        errors="replace",
        creationflags=creationflags,
    )


# ── JSON 提取辅助 ──

def _extract_json(text: str) -> Optional[Any]:
    """从文本中提取并解析 JSON（支持对象和数组）"""
    text = text.strip()
    # 同时尝试解析 JSON 对象（{...}）和 JSON 数组（[...]）
    for prefix, close in [("{", "}"), ("[", "]")]:
        if text.startswith(prefix):
            depth = 0
            start = -1
            i = 0
            while i < len(text):
                ch = text[i]
                # 跳过字符串字面量（避免括号嵌套在字符串中干扰计数）
                if ch == '"':
                    i += 1
                    while i < len(text):
                        if text[i] == '\\':
                            i += 2  # 跳过转义字符
                            continue
                        if text[i] == '"':
                            break
                        i += 1
                elif ch == prefix:
                    if start < 0:
                        start = i
                    depth += 1
                elif ch == close:
                    depth -= 1
                    if depth == 0 and start >= 0:
                        try:
                            return json.loads(text[start:i+1])
                        except json.JSONDecodeError:
                            break
                i += 1
    # 尝试代码块中的 JSON
    m = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1).strip())
        except json.JSONDecodeError:
            pass
    # 尝试正则提取对象
    m = re.search(r"(\{.*?\})", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    # 尝试正则提取数组
    m = re.search(r"(\[.*?\])", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    return None


# ── 句子组合辅助 ──

def _compose_sentences(sentences: List[str]) -> str:
    parts = []
    for s in sentences:
        s = s.strip()
        if not s:
            continue
        if parts and is_cjk(s[0]):
            parts.append(s)
        elif parts:
            parts.append(" " + s)
        else:
            parts.append(s)
    return "".join(parts)


def _nonempty_done(sent_trans: Dict[int, str]) -> Dict[str, str]:
    """断点 state 只持久化非空译文，避免空串污染后续续翻。"""
    return {
        str(k): v.strip()
        for k, v in sent_trans.items()
        if isinstance(v, str) and v.strip()
    }


def _apply_batch_translations(
    batch: List[str],
    translations: List[Dict],
    t2g: Dict[str, List[int]],
    sent_trans: Dict[int, str],
    cache: dict,
    cache_lock: Lock,
    model: str,
) -> List[Tuple[str, str]]:
    """把一批 API 结果写回 sent_trans / cache。

    优先用 id 对齐；若 id 缺失或错乱，按返回顺序对齐 batch。
    空译文不写缓存、不覆盖已有非空结果。返回成功应用的 (原文, 译文) 列表。
    """
    applied: List[Tuple[str, str]] = []
    if not translations:
        return applied

    # 规范化字段
    norm: List[Dict] = []
    for item in translations:
        if not isinstance(item, dict):
            continue
        it = dict(item)
        if not it.get("zh") and it.get("en"):
            it["zh"] = it["en"]
        if not it.get("zh") and it.get("text"):
            it["zh"] = it["text"]
        if not it.get("zh") and it.get("translation"):
            it["zh"] = it["translation"]
        norm.append(it)

    # id → 原文
    by_id: Dict[int, str] = {}
    for it in norm:
        sid = it.get("id", 0)
        try:
            sid = int(sid)
        except (TypeError, ValueError):
            continue
        zh = str(it.get("zh") or "").strip()
        if 1 <= sid <= len(batch) and zh:
            by_id[sid] = zh

    # 顺序回退：当 id 覆盖不足时，按列表顺序补齐
    ordered_zh: List[str] = []
    for it in norm:
        ordered_zh.append(str(it.get("zh") or "").strip())

    for i, orig_text in enumerate(batch):
        zh = by_id.get(i + 1, "")
        if not zh and i < len(ordered_zh):
            zh = ordered_zh[i]
        if not zh:
            continue
        key = sentence_cache_key(orig_text, model, True)
        with cache_lock:
            cache[key] = zh
        for gsid in t2g.get(orig_text, []):
            # 不覆盖已有更好结果
            if not str(sent_trans.get(gsid, "")).strip():
                sent_trans[gsid] = zh
            elif sent_trans.get(gsid) == orig_text and zh != orig_text:
                sent_trans[gsid] = zh
        applied.append((orig_text, zh))
    return applied


def _reassemble_blocks(blocks: List[SubtitleBlock], flat: List[Tuple[int, int, str]],
                       sent_trans: Dict[int, str]) -> List[str]:
    """将句子级翻译结果拼回字幕块级。

    规则：
    - 全部句有非空译文 → 拼合译文
    - 部分句有译文 → 有译用译、无译保留该句原文再拼合（不再整块丢弃）
    - 全部无译文 → 回退整块原文
    """
    gsid_to_bidx: Dict[int, List[int]] = {}
    gsid_to_orig: Dict[int, str] = {}
    for gsid, bidx, sent in flat:
        gsid_to_bidx.setdefault(bidx, []).append(gsid)
        gsid_to_orig[gsid] = sent
    result: List[str] = []
    for bidx, block in enumerate(blocks):
        sids = gsid_to_bidx.get(bidx, [])
        if not sids:
            result.append(block.text)
            continue
        pieces: List[str] = []
        any_zh = False
        for s in sids:
            zh = str(sent_trans.get(s, "") or "").strip()
            if zh:
                pieces.append(zh)
                if zh != gsid_to_orig.get(s, ""):
                    any_zh = True
            else:
                pieces.append(gsid_to_orig.get(s, ""))
        if any_zh or (pieces and all(str(sent_trans.get(s, "")).strip() for s in sids)):
            result.append(_compose_sentences(pieces))
        else:
            result.append(block.text)
    return result
