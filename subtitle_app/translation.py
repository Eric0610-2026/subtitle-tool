#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
翻译客户端：句子级缓存 + 批量翻译 + 断点续翻 + 403 fallback
"""

import json
import logging
import re
import time
import urllib.error
import urllib.request
import concurrent.futures
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from threading import Lock
from typing import Any, Callable, Dict, List, Optional, Tuple

from .config import cfg
from .local_service import local_model_name, translation_endpoint
from .srt_utils import (
    SubtitleBlock, split_sentences, sentence_cache_key,
    load_json, save_json, is_cjk,
)

logger = logging.getLogger(__name__)

# ── 进程级共享翻译缓存 ──
# 并行流水线中每个文件会创建独立的 TranslationClient，若各自加载/写回同一
# cache 文件，后写会覆盖先写的增量（缓存条目丢失、重复翻译）。
# 这里把缓存提升为进程级单例 + 全局锁：所有 client 共享同一份内存 dict，
# 写盘时串行化，彻底消除互相覆盖。
_shared_cache_lock = Lock()
_shared_cache: Dict[str, str] = {}
_shared_cache_path: Optional[Path] = None


def _get_shared_cache(cache_path: Path) -> Dict[str, str]:
    """返回绑定到 cache_path 的进程级共享缓存 dict（懒加载）。"""
    global _shared_cache, _shared_cache_path
    with _shared_cache_lock:
        if _shared_cache_path != cache_path:
            _shared_cache = load_json(cache_path, {})
            _shared_cache_path = cache_path
        return _shared_cache


def shared_cache_snapshot() -> Dict[str, str]:
    """当前共享缓存的快照（缓存管理对话框展示/落盘用）。"""
    with _shared_cache_lock:
        return dict(_shared_cache)


def remove_shared_cache_entries(keys) -> int:
    """从进程级共享缓存移除指定条目（缓存管理对话框逐条删除用）。

    必须就地修改内存 dict：已创建的 client 都持有同一对象的引用，
    若整体重新赋值，它们的写入会落回旧 dict，被删条目在下次写盘时复活。
    """
    removed = 0
    with _shared_cache_lock:
        for k in keys:
            if _shared_cache.pop(k, None) is not None:
                removed += 1
    return removed


def clear_shared_cache() -> None:
    """清空进程级共享缓存（缓存管理对话框「清空缓存」用；就地清空，理由同上）。"""
    with _shared_cache_lock:
        _shared_cache.clear()


# ── 常量（从 config.json 读取）──

API_TIMEOUT = cfg.translation.api_timeout
API_RETRY_COUNT = cfg.translation.retry_count
API_RETRY_BASE = cfg.translation.retry_base_delay
MAX_CACHE_ENTRIES = cfg.translation.max_cache_entries
PARAGRAPH_GAP = cfg.translation.paragraph_gap_seconds
CONTEXT_WINDOW = cfg.translation.context_window
MAX_RECURSION_DEPTH = getattr(cfg.translation, "max_recursion_depth", 5)
# 熔断：连续 N 批没有任何有效译文 → 判定 API 不可用/全部拒译，中止本文件
#（防止死 API 或全部拒译时把整批拖进漫长的逐句补翻）
MAX_EMPTY_BATCHES = 3
# 补翻兜底：连续 N 句补翻仍无有效译文 → 放弃剩余补翻（API 大概率不可用）
MISSING_FIX_MAX_CONSEC_FAIL = 20

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


class ApiUnavailableError(RuntimeError):
    """本机翻译服务不可达/请求超时等「服务暂时不可用」错误。

    与普通失败的区别：拆小批重试无济于事（问题不在批次大小），
    _translate_batch 收到后直接抛出，不进入递归拆分，让上层快速失败。
    """
    pass


@contextmanager
def _executor_scope(max_workers: int):
    """ThreadPoolExecutor 上下文：异常/停止时立即释放，不等待在途批次。

    默认 `with ThreadPoolExecutor` 的 __exit__ 会 shutdown(wait=True)，
    等待所有已提交任务跑完——正在执行的批次阻塞在 API 请求
    （最多 api_timeout=600s）无法中断，用户停止/熔断后 UI 会被拖住。
    这里改为：正常完成才等待（此时已无在途任务，立即返回）；
    异常路径 shutdown(wait=False, cancel_futures=True) 立即返回。
    在途请求无法中断，会在后台自然结束（工作线程非 daemon，但通常
    本地模型单请求秒级完成；极端超时场景下最多拖到 api_timeout）。
    """
    ex = ThreadPoolExecutor(max_workers=max_workers)
    try:
        yield ex
    except BaseException:
        ex.shutdown(wait=False, cancel_futures=True)
        raise
    else:
        ex.shutdown(wait=True)


class TranslationStopped(RuntimeError):
    """用户主动停止：翻译循环检测到 stop_check 后抛出，调用方静默处理（不算失败）。"""
    pass


def _extract_error(resp: dict) -> str:
    """从本地翻译服务响应中提取错误消息

    本地 llama-server 遵循标准 OpenAI 错误格式：{"error": {"message": "..."}}。
    不做多厂商兼容——本项目不存在外部 API。
    """
    err = resp.get("error")
    if isinstance(err, dict):
        msg = err.get("message", "") or err.get("msg", "") or str(err)
        return msg or ""
    if isinstance(err, str):
        return err
    return ""


# ── TranslationClient ──


class TranslationClient:
    """翻译客户端：句子级缓存 + 批量翻译 + 断点续翻

    端点固定为本机 llama-server（local_service.translation_endpoint），
    构造函数不接受任何地址参数，因此无需 API 密钥与鉴权头，
    也就不存在把请求发往外部服务器的代码路径。
    """

    def __init__(self, cache_path: Path, post_ui: Callable,
                 batch_size: int = None, target_lang: str = "zh",
                 send_all: bool = False):
        if batch_size is None:
            batch_size = cfg.translation.batch_size
        # 钳位：0/负数会导致分批 `range(0, len, 0)` 抛 ValueError、`// 0` 抛 ZeroDivisionError
        try:
            self.batch_size = max(1, int(batch_size))
        except (TypeError, ValueError):
            self.batch_size = max(1, int(cfg.translation.batch_size or 20))
        self.send_all = send_all
        self.endpoint = translation_endpoint()   # 固定本机 llama-server，无外部地址
        self.model = local_model_name()
        self.target_lang = target_lang
        self.system_prompt = make_prompt(target_lang)
        # 进程级共享缓存：并发流水线中所有 client 共享同一内存 dict，
        # 配合全局锁保证写盘串行，避免后写覆盖先写导致缓存丢失
        self.cache: Dict[str, str] = _get_shared_cache(cache_path)
        self.cache_path = cache_path
        self.post_ui = post_ui
        self._cache_lock = _shared_cache_lock


    def translate_blocks(self, blocks: List[SubtitleBlock], source_lang: str,
                         is_bilingual: bool, state_path: Optional[Path] = None,
                         translation_concurrency: int = 1,
                         stop_check: Optional[Callable[[], bool]] = None) -> List[str]:
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
            key = sentence_cache_key(sent, self.model, is_bilingual)
            cached = self.cache.get(key)
            if isinstance(cached, str) and cached.strip():
                sent_trans[gsid] = cached.strip()
                with self._cache_lock:
                    if key in self.cache:
                        # 触碰一次（重新插入移到末尾=最近使用端），淘汰时才按 LRU 而非插入序
                        self.cache[key] = self.cache.pop(key)
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
        # 段落上下文：段号 → 该段已翻译完成的译文（作为后续批次的「上文」）。
        # worker 内先等本段前一批完成（para_gate 的 future 链），再把译文入表，
        # 保证段内严格有序；段间互不等待，保留并发。para_lock 保护并发追加。
        para_lock = Lock()
        para_context: Dict[int, List[str]] = {}
        para_gate: Dict[int, concurrent.futures.Future] = {}
        # 缓存命中/断点恢复的译文也算已完成的上文：预填，续翻后上下文才连续
        for gsid, zh in sent_trans.items():
            if zh and gsid < len(gsid_to_para):
                p = gsid_to_para[gsid]
                para_context.setdefault(p, []).append(zh)
        for p in para_context:
            para_context[p] = para_context[p][-CONTEXT_WINDOW:]

        def _ctx_text(paras) -> str:
            """拼接批次涉及段落的译文上文（每段截取 CONTEXT_WINDOW 条）"""
            with para_lock:
                lines = []
                for p in sorted(paras):
                    for ctx in para_context.get(p, [])[-CONTEXT_WINDOW:]:
                        lines.append(f"（上文）{ctx}")
            return "\n".join(lines) + "\n" if lines else ""

        with _executor_scope(translation_concurrency) as executor:
            batch_futures: List[tuple] = []
            self.post_ui({
                "type": "log",
                "message": f"开始提交翻译批次：共 {total_batches} 批（{len(unique_texts)} 句）",
                "level": "INFO",
            })
            for batch_idx in range(0, len(unique_texts), effective_batch_size):
                if stop_check is not None and stop_check():
                    self._abort_translation(batch_futures, state_path,
                                            sent_originals, sent_trans)
                    raise TranslationStopped()
                batch = unique_texts[batch_idx:batch_idx + effective_batch_size]
                batch_id = batch_idx // self.batch_size + 1
                para_ids = set()
                for text in batch:
                    for gsid in text_to_gsid.get(text, []):
                        if gsid < len(gsid_to_para):
                            para_ids.add(gsid_to_para[gsid])
                # gate = 本批涉及段落的「前一批」future；提交时取好传进 worker，
                # worker 不读 para_gate，避免读到主线程后续覆盖的 gate
                gates = [para_gate[p] for p in para_ids if p in para_gate]

                def _job(batch=batch, gates=gates, para_ids=para_ids):
                    # 用户停止：不再发起新的 API 调用（在途请求无法中断，自然结束）
                    if stop_check is not None and stop_check():
                        raise TranslationStopped()
                    # 段内有序：前一批的译文已被它的 worker 应用进 para_context
                    for g in gates:
                        g.result()
                    context_text = _ctx_text(para_ids)
                    translations = self._translate_batch(batch, context_text, 0)
                    applied = _apply_batch_translations(
                        batch, translations, text_to_gsid, sent_trans,
                        self.cache, self._cache_lock, self.model, is_bilingual)
                    with para_lock:
                        for orig_text, zh_text in applied:
                            if not zh_text:
                                continue
                            for gsid in text_to_gsid.get(orig_text, []):
                                if gsid < len(gsid_to_para):
                                    para = gsid_to_para[gsid]
                                    para_context.setdefault(para, []).append(zh_text)
                                    para_context[para] = para_context[para][-CONTEXT_WINDOW:]
                    # 返回本批有效译文数（0 = 整批无产出，供空批熔断统计）
                    return sum(1 for _, zh in applied if zh)

                future = executor.submit(_job)
                for p in para_ids:
                    para_gate[p] = future
                batch_futures.append((future, batch_id))

            # 按提交顺序收结果（state 串行写盘；异常/停止时取消未开始的批次）
            completed_count = 0
            consecutive_empty = 0
            for future, batch_id in batch_futures:
                if stop_check is not None and stop_check():
                    self._abort_translation(batch_futures, state_path,
                                            sent_originals, sent_trans)
                    raise TranslationStopped()
                # 轮询等待，每隔 15s 发送心跳防止 UI 假死
                while True:
                    try:
                        applied_count = future.result(timeout=15)
                        break
                    except (concurrent.futures.TimeoutError, TimeoutError):
                        slow_hint = "本地模型推理中"
                        self.post_ui({
                            "type": "progress",
                            "percent": (completed_count / max(total_batches, 1)) * 100,
                            "stage": "翻译",
                            "detail": f"批次 {batch_id}/{total_batches} 仍在翻译中（{slow_hint}，已完成 {completed_count}/{total_batches} 批）",
                            "total": len(blocks), "cache": len(self.cache),
                        })
                    except Exception:
                        # 批次失败（含 curl 子进程超时等非 RuntimeError 异常）：
                        # 先落盘断点，再取消未开始的批次，避免白烧 API
                        self._abort_translation(batch_futures, state_path,
                                                sent_originals, sent_trans)
                        raise
                # 空批熔断：连续多批没有任何有效译文 → API 不可用或全部拒译，
                # 继续下去只会白烧请求并拖进漫长的补翻，直接中止本文件
                if applied_count == 0:
                    consecutive_empty += 1
                    if consecutive_empty >= MAX_EMPTY_BATCHES:
                        self.post_ui({
                            "type": "log",
                            "message": f"连续 {MAX_EMPTY_BATCHES} 批无任何有效译文，"
                                       f"判定本地翻译服务不可用，中止本文件（已完成的批次已保存）",
                            "level": "ERROR",
                        })
                        self._abort_translation(batch_futures, state_path,
                                                sent_originals, sent_trans)
                        raise RuntimeError(
                            f"连续 {MAX_EMPTY_BATCHES} 批无有效译文（本地服务可能不可用或全部拒译）")
                else:
                    consecutive_empty = 0
                if state_path:
                    save_json(state_path, {
                        "done": _snapshot_done(sent_trans, self._cache_lock),
                        "originals": sent_originals,
                        "updated_at": datetime.now().isoformat(),
                    })
                completed_count += 1
                self.post_ui({
                    "type": "progress",
                    "percent": (completed_count / max(total_batches, 1)) * 100,
                    "stage": "翻译",
                    "detail": f"批次 {completed_count}/{total_batches} 完成",
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
            consec_fail = 0
            for i, (orig, gsids) in enumerate(miss_map.items(), 1):
                got_translation = False
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
                        key = sentence_cache_key(orig, self.model, is_bilingual)
                        with self._cache_lock:
                            self.cache[key] = zh
                        for gsid in gsids:
                            sent_trans[gsid] = zh
                        got_translation = True
                    elif zh:
                        # 与原文相同也写入，避免反复补翻同一句；双语组装层会处理
                        for gsid in gsids:
                            sent_trans[gsid] = zh
                    else:
                        logger.warning("单条补翻仍无结果: %r", orig[:80])
                except Exception as e:
                    logger.warning("单条补翻失败 %r: %s", orig[:80], e)
                # 熔断统计：异常/无译文/译文=原文（拒译）都算无进展，
                # 连续过多说明 API 不可用，放弃剩余补翻避免拖死整个文件
                consec_fail = 0 if got_translation else consec_fail + 1
                if consec_fail >= MISSING_FIX_MAX_CONSEC_FAIL:
                    self.post_ui({
                        "type": "log",
                        "message": f"连续 {consec_fail} 句补翻无有效译文，放弃剩余补翻"
                                   f"（{len(miss_map) - i} 句未补，无译文处将保留原文）",
                        "level": "WARNING",
                    })
                    break
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
                    "done": _snapshot_done(sent_trans, self._cache_lock),
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

    def _abort_translation(self, batch_futures: List[tuple], state_path: Optional[Path],
                           sent_originals: Dict[str, str], sent_trans: Dict[int, str]) -> None:
        """中止本轮翻译：落盘断点 + 取消尚未开始的批次（在途批次自然结束）。

        不取消的话，线程池里已提交的剩余批次会继续占用推理算力把时间跑完，
        而结果随后被丢弃。
        """
        for f, _ in batch_futures:
            f.cancel()
        self._save_cache()
        if state_path:
            save_json(state_path, {
                "done": _snapshot_done(sent_trans, self._cache_lock),
                "originals": sent_originals,
                "updated_at": datetime.now().isoformat(),
            })

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
        headers = {"Content-Type": "application/json"}
        try:
            resp_data = self._call_api(payload, headers)
        except ApiUnavailableError:
            # 网络不可达/超时：拆小批不解决问题（_call_api 内部已重试 3 次），
            # 直接失败交给上层熔断/停止，避免 2^depth 个子请求各重试一轮
            raise
        except Exception as e:
            # 修正缩进：正确处理递归拆分，增加深度限制
            if len(texts) > 1 and depth < MAX_RECURSION_DEPTH:
                logger.warning("批量翻译失败（深度 %d），拆分为小批次重试: %s", depth, e)
                return self._translate_split(texts, context, depth)
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
                return self._translate_split(texts, context, depth)
            elif depth >= MAX_RECURSION_DEPTH:
                logger.error("翻译响应解析失败，超过递归深度限制 %d，返回原文", MAX_RECURSION_DEPTH)
                return [{"id": i + 1, "zh": t} for i, t in enumerate(texts)]
            else:
                logger.warning("单句翻译响应解析失败，尝试纯文本模式: %s", e)
                return self._call_api_single_plain(texts[0])

    def _translate_split(self, texts: List[str], context: str, depth: int = 0) -> List[Dict]:
        """批量翻译在 API 侧失败时拆半重试；对第二个子批的 id 重新编号以避免回填错位。"""
        mid = len(texts) // 2
        left = self._translate_batch(texts[:mid], context, depth + 1)
        right = self._translate_batch(texts[mid:], context, depth + 1)
        # 两个子批的 id 都从 1 开始；不重排会导致按 id 回填时后批覆盖前批、译文串位。
        # 偏移必须用 mid（右半批在原批次中的真实起点）而非 len(left)：
        # 左半批 API 少返回条目时（截断丢项），len(left) < mid 会让右半批译文整体前移错位
        offset = mid
        renumbered = []
        for item in right:
            if isinstance(item, dict) and item.get("id") is not None:
                try:
                    item = {**item, "id": int(item["id"]) + offset}
                except (TypeError, ValueError):
                    pass
            renumbered.append(item)
        return left + renumbered

    def _call_api(self, payload: dict, headers: dict) -> dict:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(self.endpoint, data=data, headers=headers, method="POST")
        last_err: Optional[Exception] = None
        for attempt in range(1, API_RETRY_COUNT + 1):
            try:
                with urllib.request.urlopen(req, timeout=API_TIMEOUT) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", errors="replace")
                last_err = RuntimeError(f"HTTP {e.code}: {body[:200]}")
                logger.warning("本地服务 HTTP 错误 (尝试 %d/%d): %s", attempt, API_RETRY_COUNT, e.code)
            except urllib.error.URLError as e:
                last_err = ApiUnavailableError(f"本地服务连接失败: {e.reason}")
                logger.warning("本地服务连接失败 (尝试 %d/%d): %s", attempt, API_RETRY_COUNT, e.reason)
            except TimeoutError:
                last_err = ApiUnavailableError("本地服务请求超时")
                logger.warning("本地服务超时 (尝试 %d/%d)", attempt, API_RETRY_COUNT)
            except Exception as e:
                last_err = ApiUnavailableError(f"本地服务请求失败: {e}")
                logger.warning("API 请求异常 (尝试 %d/%d): %s", attempt, API_RETRY_COUNT, e)
            if attempt < API_RETRY_COUNT:
                delay = API_RETRY_BASE * (2 ** (attempt - 1))
                logger.info("等待 %.1f 秒后重试...", delay)
                time.sleep(delay)
        if last_err is None:
            last_err = RuntimeError("API 请求失败（无具体错误）")
        raise last_err

    def _call_api_single_plain(self, text: str) -> List[Dict]:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": "你是严谨的字幕翻译器。将以下文本翻译为简体中文，只返回译文，不要其他内容。"},
                {"role": "user", "content": text},
            ],
            "temperature": cfg.translation.temperature, "stream": False,
        }
        headers = {"Content-Type": "application/json"}
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
        with _shared_cache_lock:
            if len(self.cache) > MAX_CACHE_ENTRIES:
                # LRU 裁剪：命中即触碰（重新插入移到末尾），dict 头部即最久未用；
                # 超额时裁掉一半最久未用的，保留高频句（旧 FIFO 会误裁高频但早插入的句子）
                excess = len(self.cache) - MAX_CACHE_ENTRIES // 2
                keys_to_remove = list(self.cache.keys())[:excess]
                for k in keys_to_remove:
                    del self.cache[k]
            save_json(self.cache_path, self.cache)

    def get_cache_size(self) -> int:
        return len(self.cache)


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
    # 尝试正则提取对象（贪婪：匹配到最后一个 }，容忍 JSON 前有说明文字；
    # 非贪婪会在第一个 } 截断导致必然解析失败）
    m = re.search(r"(\{.*\})", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    # 尝试正则提取数组（同为贪婪匹配）
    m = re.search(r"(\[.*\])", text, re.DOTALL)
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


def _snapshot_done(sent_trans: Dict[int, str], lock: Lock) -> Dict[str, str]:
    """在锁内对非空译文做快照再落盘。

    保存断点时其它批次的 worker 可能正并发写入 sent_trans，
    主线程直接迭代会抛 "dictionary changed size during iteration"。
    """
    with lock:
        return _nonempty_done(sent_trans)


def _apply_batch_translations(
    batch: List[str],
    translations: List[Dict],
    t2g: Dict[str, List[int]],
    sent_trans: Dict[int, str],
    cache: dict,
    cache_lock: Lock,
    model: str,
    is_bilingual: bool = True,
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

    # 顺序回退仅当返回值与输入条数一致时使用，防止条数不足时把相邻句子译文错位对齐
    if len(norm) == len(batch):
        ordered_zh: List[str] = [str(it.get("zh") or "").strip() for it in norm]
    else:
        ordered_zh: List[str] = []

    for i, orig_text in enumerate(batch):
        zh = by_id.get(i + 1, "")
        if not zh and ordered_zh and i < len(ordered_zh):
            zh = ordered_zh[i]
        if not zh:
            continue
        if zh == orig_text:
            # 模型把原文当译文返回（拒译/未答）：不当有效译文，不写缓存，留待单条补翻
            applied.append((orig_text, ""))
            continue
        key = sentence_cache_key(orig_text, model, is_bilingual)
        # sent_trans 与 cache 同锁保护：主线程保存断点时会迭代 sent_trans，
        # worker 并发写入必须持同一把锁，否则迭代侧抛 "dictionary changed size"
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
