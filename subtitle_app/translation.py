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

    处理两种常见兼容性问题：
    1. 商汤等：choices/usage 包裹在 data 字段下（非标准 OpenAPI）
    2. 商汤等：choices[i].message 是字符串而非 {"content": "..."}
    """
    resp = dict(resp)  # 不修改原始 dict
    # 1) 当顶层无 choices 但 data 中有时，提升到顶层
    if "choices" not in resp and "data" in resp and isinstance(resp["data"], dict):
        data = resp["data"]
        if "choices" in data:
            resp["choices"] = data["choices"]
        if "usage" in data and "usage" not in resp:
            resp["usage"] = data["usage"]
        if "id" in data and "id" not in resp:
            resp["id"] = data["id"]
    # 2) 标准化 choices[i].message 为对象格式
    for choice in resp.get("choices", []):
        if isinstance(choice, dict):
            msg = choice.get("message")
            if isinstance(msg, str):
                choice["message"] = {"content": msg}
    return resp


# ── TranslationClient ──


class TranslationClient:
    """翻译客户端：句子级缓存 + 批量翻译 + 断点续翻 + 403 fallback"""

    def __init__(self, api_url: str, api_key: str, model: str, cache_path: Path,
                 post_ui: Callable, batch_size: int = None, target_lang: str = "zh"):
        if batch_size is None:
            batch_size = cfg.translation.batch_size
        self.batch_size = batch_size
        self.api_url = api_url
        self.api_key = api_key
        self.model = model
        self.target_lang = target_lang
        self.system_prompt = make_prompt(target_lang)
        self.cache: Dict[str, str] = load_json(cache_path, {})
        self.cache_path = cache_path
        self.post_ui = post_ui
        self._usage = {"prompt_tokens": 0, "completion_tokens": 0,
                       "prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 0}
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

        # Step 2: 缓存命中 + 断点恢复
        sent_trans: Dict[int, str] = {}
        # 构建原文映射（用于断点恢复时的内容校验）
        sent_originals: Dict[str, str] = {str(gsid): sent for gsid, _, sent in flat}
        state: Dict = {}
        if state_path and state_path.exists():
            state = load_json(state_path, {})
            done = state.get("done", {})
            saved_originals = state.get("originals", {})
            for entry in flat:
                gsid = entry[0]
                gsid_str = str(gsid)
                if gsid_str in done and saved_originals.get(gsid_str) == sent_originals.get(gsid_str):
                    sent_trans[gsid] = done[gsid_str]

        to_translate: List[Tuple[int, str]] = []
        for gsid, bidx, sent in flat:
            if gsid in sent_trans:
                continue
            key = sentence_cache_key(sent, self.model, True)
            if key in self.cache:
                sent_trans[gsid] = self.cache[key]
            else:
                sent_trans[gsid] = ""
                to_translate.append((gsid, sent))

        # 去重
        text_to_gsid: Dict[str, List[int]] = {}
        for gsid, sent in to_translate:
            text_to_gsid.setdefault(sent, []).append(gsid)
        unique_texts = list(text_to_gsid.keys())
        if not unique_texts:
            # 所有句子都命中缓存/断点状态，直接组装译文
            # 先构建索引，O(n) 避免 O(n²)
            gsid_to_bidx: Dict[int, List[int]] = {}
            for gsid, bidx, _sent in flat:
                gsid_to_bidx.setdefault(bidx, []).append(gsid)
            result_texts: List[str] = []
            for bidx, block in enumerate(blocks):
                block_sids = gsid_to_bidx.get(bidx, [])
                translated_sents = []
                missing = False
                for sid in block_sids:
                    t = sent_trans.get(sid, "")
                    if t:
                        translated_sents.append(t)
                    else:
                        missing = True
                if missing or not translated_sents:
                    result_texts.append(block.text)
                else:
                    combined = _compose_sentences(translated_sents)
                    result_texts.append(combined)
            self.post_ui({
                "type": "progress", "percent": 100,
                "stage": "翻译", "detail": f"翻译完成（全部命中缓存，共 {len(blocks)} 条）",
                "total": len(blocks), "cache": len(self.cache),
            })
            return result_texts

        # Step 3: 批量翻译（多线程并发 API 调用）
        total_batches = (len(unique_texts) + self.batch_size - 1) // self.batch_size
        para_context: Dict[int, List[str]] = {}

        with ThreadPoolExecutor(max_workers=translation_concurrency) as executor:
            batch_futures: List[tuple] = []
            for batch_idx in range(0, len(unique_texts), self.batch_size):
                batch = unique_texts[batch_idx:batch_idx + self.batch_size]
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
                    "type": "progress", "percent": ((batch_id - 0.5) / max(total_batches, 1)) * 100,
                    "stage": "翻译", "detail": f"翻译批次 {batch_id}/{total_batches}（{len(batch)} 句）",
                    "total": len(blocks), "cache": len(self.cache),
                })
                future = executor.submit(self._translate_batch, batch, context_text, 0)
                batch_futures.append((future, batch, text_to_gsid, batch_id, main_para))

            # 按提交顺序处理结果（保证段落上下文连续性）
            for future, batch, t2g, batch_id, main_para in batch_futures:
                # 轮询等待，每隔 15s 发送心跳防止 UI 假死
                while True:
                    try:
                        translations = future.result(timeout=15)
                        break
                    except TimeoutError:
                        self.post_ui({
                            "type": "progress",
                            "percent": ((batch_id - 0.5) / max(total_batches, 1)) * 100,
                            "stage": "翻译",
                            "detail": f"批次 {batch_id}/{total_batches} 仍在翻译中（API 响应较慢）...",
                            "total": len(blocks), "cache": len(self.cache),
                        })
                try:
                    translations = future.result()
                except RuntimeError:
                    self._save_cache()
                    if state_path:
                        save_json(state_path, {
                            "done": sent_trans,
                            "originals": sent_originals,
                            "updated_at": datetime.now().isoformat(),
                        })
                    raise
                for item in translations:
                    # 兼容混元：混元常把译文放 en 而非 zh 字段
                    if not item.get("zh") and item.get("en"):
                        item["zh"] = item["en"]
                    sid = item.get("id", 0)
                    zh_text = item.get("zh", "")
                    if 1 <= sid <= len(batch):
                        orig_text = batch[sid - 1]
                        key = sentence_cache_key(orig_text, self.model, True)
                        with self._cache_lock:
                            self.cache[key] = zh_text
                        for gsid in t2g.get(orig_text, []):
                            sent_trans[gsid] = zh_text
                for item in translations:
                    zh_text = item.get("zh", "")
                    if not zh_text:
                        continue
                    sid = item.get("id", 0)
                    if 1 <= sid <= len(batch):
                        orig_text = batch[sid - 1]
                        for gsid in t2g.get(orig_text, []):
                            if gsid < len(gsid_to_para):
                                para = gsid_to_para[gsid]
                                para_context.setdefault(para, []).append(zh_text)
                                para_context[para] = para_context[para][-CONTEXT_WINDOW:]
                if state_path:
                    save_json(state_path, {
                        "done": sent_trans,
                        "originals": sent_originals,
                        "updated_at": datetime.now().isoformat(),
                    })

        # Step 4: 立即告知 UI 翻译完成（不影响后续 I/O）
        self.post_ui({
            "type": "progress", "percent": 100,
            "stage": "翻译", "detail": f"翻译完成（共 {len(blocks)} 条）",
            "total": len(blocks), "cache": len(self.cache),
        })

        # 拼回块（先构建索引，O(n) 避免 O(n²)）
        gsid_to_bidx: Dict[int, List[int]] = {}
        for gsid, bidx, _sent in flat:
            gsid_to_bidx.setdefault(bidx, []).append(gsid)
        result_texts: List[str] = []
        for bidx, block in enumerate(blocks):
            block_sids = gsid_to_bidx.get(bidx, [])
            translated_sents = []
            missing = False
            for sid in block_sids:
                t = sent_trans.get(sid, "")
                if t:
                    translated_sents.append(t)
                else:
                    missing = True
            if missing or not translated_sents:
                result_texts.append(block.text)
            else:
                combined = _compose_sentences(translated_sents)
                result_texts.append(combined)

        # 后台 I/O（保存缓存、报告费用，用户已看到 100%）
        self._save_cache()
        self._report_cost()
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
        usage = resp_data.get("usage", {})
        self._usage["prompt_tokens"] += usage.get("prompt_tokens", 0)
        self._usage["completion_tokens"] += usage.get("completion_tokens", 0)
        self._usage["prompt_cache_hit_tokens"] += usage.get("prompt_cache_hit_tokens", 0)
        self._usage["prompt_cache_miss_tokens"] += usage.get("prompt_cache_miss_tokens", 0)
        return self._parse_translation_response(resp_data)

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
                if e.code == 403:
                    raise ApiForbiddenError("API 返回 403")
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
            content = resp_data.get("choices", [{}])[0].get("message", {}).get("content", "")
            if content:
                return [{"id": 1, "zh": content.strip()}]
        except Exception as e:
            logger.warning("纯文本单句翻译也失败: %s，返回原文", e)
        return [{"id": 1, "zh": text}]

    def _parse_translation_response(self, resp_data: dict) -> List[Dict]:
        items = None
        try:
            content = resp_data.get("choices", [{}])[0].get("message", {}).get("content", "")
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
            items = resp_data.get("items") or resp_data.get("translations") or resp_data.get("result")
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
            logger.error("无法解析翻译响应: %s", json.dumps(resp_data, ensure_ascii=False)[:300])
            raise RuntimeError("无法解析翻译响应（请检查 API Key 和模型名称）")
        return items if isinstance(items, list) else []

    def _report_cost(self) -> None:
        """报告本次翻译的 token 消耗和费用估算"""
        u = self._usage
        total_in = u["prompt_tokens"]
        total_out = u["completion_tokens"]
        cache_hit = u["prompt_cache_hit_tokens"]
        cache_miss = u["prompt_cache_miss_tokens"]
        hit_rate = (cache_hit / total_in * 100) if total_in > 0 else 0

        if total_in == 0 and total_out == 0:
            self.post_ui({"type": "log", "message": "本次翻译全部命中缓存，未产生 API 费用", "level": "INFO"})
            return

        lines = [
            f"翻译 Token 消耗: 输入 {total_in:,} (缓存命中 {cache_hit:,}/{cache_miss:,}, {hit_rate:.1f}%), 输出 {total_out:,}"
        ]
        input_cost = (cache_miss / 1000) * cfg.pricing.input_per_1k
        cache_cost = (cache_hit / 1000) * cfg.pricing.cache_hit_per_1k
        output_cost = (total_out / 1000) * cfg.pricing.output_per_1k
        estimated_cost = input_cost + cache_cost + output_cost
        lines.append(
            f"估算费用: ¥{estimated_cost:.4f} (输入 ¥{input_cost:.4f} + 缓存 ¥{cache_cost:.4f} + 输出 ¥{output_cost:.4f})"
        )
        for line in lines:
            self.post_ui({"type": "log", "message": line, "level": "INFO"})

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

    def get_cost_info(self) -> dict:
        u = self._usage
        total_in = u["prompt_tokens"]
        total_out = u["completion_tokens"]
        cache_hit = u["prompt_cache_hit_tokens"]
        cache_miss = u["prompt_cache_miss_tokens"]
        input_cost = (cache_miss / 1000) * cfg.pricing.input_per_1k
        cache_cost = (cache_hit / 1000) * cfg.pricing.cache_hit_per_1k
        output_cost = (total_out / 1000) * cfg.pricing.output_per_1k
        return {
            "prompt_tokens": total_in,
            "completion_tokens": total_out,
            "cache_hit_tokens": cache_hit,
            "cache_miss_tokens": cache_miss,
            "input_cost": round(input_cost, 6),
            "cache_cost": round(cache_cost, 6),
            "output_cost": round(output_cost, 6),
            "total_cost": round(input_cost + cache_cost + output_cost, 6),

        }


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
            for i, ch in enumerate(text):
                if ch == prefix:
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
