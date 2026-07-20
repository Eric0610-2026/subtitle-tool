#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SRT 解析/写入、句子处理、繁简转换、字幕文件查找等工具函数
"""

import hashlib
import json
import logging
import re
import shutil
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, List, Optional, Tuple

from .config import cfg

logger = logging.getLogger(__name__)

# ── 常量（从 config.json 读取）──

VIDEO_EXTS = set(cfg.srt.video_exts)
AUDIO_EXTS = set(getattr(cfg.srt, "audio_exts", []))
SUB_EXTS = set(cfg.srt.sub_exts)
INPUT_EXTS = VIDEO_EXTS | AUDIO_EXTS | SUB_EXTS
MAX_FILENAME_STEM = cfg.srt.max_filename_stem

_ABBREVIATIONS = set(cfg.srt.abbreviations)

# 扩充的繁简转换表（常用繁体字）
_SIMPLE_T2S = {
    "爲": "为", "偽": "伪", "偉": "伟", "傳": "传", "傷": "伤",
    "僅": "仅", "優": "优", "關": "关", "係": "系", "個": "个",
    "們": "们", "會": "会", "體": "体", "學": "学", "國": "国",
    "開": "开", "發": "发", "與": "与", "時": "时", "書": "书",
    "長": "长", "門": "门", "問": "问", "說": "说", "話": "话",
    "東": "东", "樂": "乐", "氣": "气", "電": "电", "視": "视",
    "見": "见", "親": "亲", "愛": "爱", "萬": "万", "無": "无",
    "幾": "几", "從": "从", "來": "来", "兩": "两", "還": "还",
    "這": "这", "裏": "里", "後": "后", "點": "点", "麵": "面",
    "幹": "干", "準": "准", "複": "复", "復": "复", "鬥": "斗",
    "儘": "尽", "盡": "尽", "歷": "历", "曆": "历", "願": "愿",
    "響": "响", "黨": "党", "當": "当", "噹": "当", "髒": "脏",
    "臟": "脏", "臺": "台", "檯": "台", "颱": "台", "灣": "湾",
    "雲": "云", "纔": "才", "採": "采", "衆": "众", "種": "种",
    "蟲": "虫", "衝": "冲", "劃": "划",
    "曬": "晒", "灑": "洒", "網": "网", "羣": "群", "峯": "峰",
    "跡": "迹", "蹟": "迹", "勳": "勋", "嘆": "叹", "啓": "启",
    "匯": "汇", "彙": "汇", "徧": "遍", "佈": "布", "餵": "喂",
    "煙": "烟", "菸": "烟", "遊": "游", "餘": "余", "鬱": "郁",
    "慾": "欲", "讚": "赞", "證": "证", "癥": "症", "糉": "粽",
    "藴": "蕴",
    "產": "产", "業": "业", "華": "华",
    "單": "单", "雙": "双", "參": "参", "變": "变", "條": "条",
    "過": "过", "達": "达", "進": "进", "運": "运",
    "動": "动", "務": "务", "處": "处", "備": "备",
    "價": "价", "環": "环", "選": "选", "擇": "择", "據": "据",
    "數": "数", "較": "较", "車": "车", "輛": "辆", "輪": "轮",
    "軟": "软", "軍": "军", "載": "载", "輕": "轻", "轉": "转",
    "輸": "输", "農": "农", "豐": "丰", "識": "识", "語": "语",
    "讀": "读", "請": "请", "議": "议", "謝": "谢", "講": "讲",
    "護": "护", "獲": "获", "獨": "独", "獻": "献",
    "狀": "状", "將": "将", "獎": "奖", "醬": "酱", "壯": "壮",
    "裝": "装", "節": "节", "爺": "爷", "傑": "杰", "經": "经",
    "勁": "劲", "徑": "径", "莖": "茎", "逕": "迳", "頸": "颈",
    "觀": "观", "歡": "欢", "權": "权", "勸": "劝", "歎": "叹",
    "難": "难", "漢": "汉", "艱": "艰", "熾": "炽",
    "職": "职", "織": "织", "幟": "帜", "積": "积", "績": "绩",
    "責": "责", "漬": "渍", "帻": "帻", "賊": "贼", "鴨": "鸭",
    "壓": "压", "莊": "庄", "嚴": "严", "廠": "厂", "場": "场",
    "楊": "杨", "暢": "畅", "樣": "样", "鹽": "盐", "監": "监",
    "鑒": "鉴", "鑑": "鉴", "覽": "览", "攬": "揽", "纜": "缆",
    "榄": "榄", "暫": "暂", "鑿": "凿", "棗": "枣", "叢": "丛",
    "縱": "纵", "聰": "聪", "總": "总", "樅": "枞", "鏨": "錾",
    "倉": "仓", "艙": "舱", "蒼": "苍", "傖": "伧", "瘡": "疮",
    "搶": "抢", "槍": "枪", "嗆": "呛", "戧": "戗", "創": "创",
    "滄": "沧", "愴": "怆", "鎗": "锵", "層": "层", "贈": "赠",
    "罾": "罾", "增": "增", "憎": "憎", "繒": "缯", "矰": "矰",
    "曾": "曾",
}

_JAPANESE_SPECIFIC_KANJI = {
    '駅', '辻', '込', '畑', '峠', '栃', '埼', '榊', '笹', '匂',
    '丼', '働', '塀', '姫', '穂', '瀬', '咲', '鋸', '凧', '凩',
    '匁', '俣', '樺', '櫛', '脇', '禿', '鱈', '鮭', '鯛', '鰻',
}

# ── 时间工具 ──

def seconds_to_srt_time(sec: float) -> str:
    if sec is None or sec < 0:
        return "00:00:00,000"
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = int(sec % 60)
    ms = int(round((sec - int(sec)) * 1000))
    if ms >= 1000:
        ms = 999
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def srt_time_to_seconds(t_str: str) -> float:
    m = re.match(r"(\d+):(\d{1,2}):(\d{1,2})[,.](\d{1,3})", t_str.strip())
    if not m:
        return 0.0
    h, mi, s, ms = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))
    # 处理毫秒位数不一致的情况（补零）
    ms_str = m.group(4).ljust(3, '0')[:3]
    ms = int(ms_str)
    return h * 3600 + mi * 60 + s + ms / 1000


def fmt_duration(sec: Optional[float]) -> str:
    if sec is None or sec == float("inf"):
        return "--:--"
    total_s = int(sec)
    h, m, s = total_s // 3600, (total_s % 3600) // 60, total_s % 60
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def estimate_eta(start_ts: float, fraction: float) -> Tuple[str, str]:
    if fraction <= 0:
        return "--:--", "--:--"
    elapsed = time.time() - start_ts
    total_est = elapsed / fraction if fraction > 0 else 0
    remain = max(0, total_est - elapsed)
    finish_time = datetime.fromtimestamp(start_ts + total_est)
    return fmt_duration(remain), finish_time.strftime("%H:%M")


# ── JSON 读写 ──

def load_json(path: Path, default: Any = None) -> Any:
    if default is None:
        default = {}
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        logger.warning("JSON 解析失败 %s: %s", path, e)
        return default
    except PermissionError as e:
        logger.warning("无权限读取 %s: %s", path, e)
        return default
    except OSError as e:
        logger.warning("读取文件失败 %s: %s", path, e)
        return default


def save_json(path: Path, data: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            tmp.replace(path)
        except OSError:
            # 跨文件系统回退：复制后删除临时文件
            shutil.copy2(str(tmp), str(path))
            tmp.unlink(missing_ok=True)
    except Exception as e:
        logger.error("保存 JSON 失败 %s: %s", path, e)
        # 尝试清理临时文件
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        raise


# ── 数据模型 ──

@dataclass
class SubtitleBlock:
    index: int
    start: float
    end: float
    text: str

    @property
    def timing(self) -> str:
        return f"{seconds_to_srt_time(self.start)} --> {seconds_to_srt_time(self.end)}"


# ── SRT 解析与写入 ──

# 改进的 SRT 解析正则：
# - 支持任意小时位数（\d+ 而非 \d{1,2}），兼容超长视频
# - 时间戳后允许更多空白字符
# - 字幕文本支持包含单独的 \r（非连续换行）
# - 使用宽松的行分割策略
SRT_BLOCK_RE = re.compile(
    r"(?:(\d+)\s*(?:\r?\n|\r))?"  # 可选序号（支持 \r\n, \n, \r）
    r"(\d+:\d{1,2}:\d{1,2}[,.]\d{1,3})\s*-->\s*(\d+:\d{1,2}:\d{1,2}[,.]\d{1,3})\s*(?:\r?\n|\r)"  # 时间戳
    r"((?:(?!(?:\r?\n|\r){2}).)+)",  # 文本内容（直到遇到空行）
    re.DOTALL,
)


def parse_srt(path: Path) -> List[SubtitleBlock]:
    text = path.read_text(encoding="utf-8-sig")
    # 统一换行符
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    blocks = []
    idx_counter = 0
    for m in SRT_BLOCK_RE.finditer(text):
        idx_counter += 1
        idx = int(m.group(1)) if m.group(1) else idx_counter
        start = srt_time_to_seconds(m.group(2))
        end = srt_time_to_seconds(m.group(3))
        content = m.group(4).strip()
        blocks.append(SubtitleBlock(index=idx, start=start, end=end, text=content))
    return blocks


def sanitize_blocks(blocks: List[SubtitleBlock],
                     min_duration: float = 0.5) -> List[SubtitleBlock]:
    """就地修正字幕块时间戳，保证：start>=0、时间单调不回退、end>start 且至少 min_duration。

    这能避免转写结果中偶发的 end<=start / 时间回退导致 SRT 非法、进而 ffmpeg 内嵌失败。"""
    prev_end = 0.0
    for b in blocks:
        start = max(0.0, b.start)
        if start < prev_end:
            start = prev_end
        end = max(start + min_duration, b.end)
        if end <= start:
            end = start + min_duration
        if end < prev_end:
            end = prev_end + 0.01
        b.start, b.end = start, end
        prev_end = end
    return blocks


def write_srt(path: Path, blocks: List[SubtitleBlock], texts: List[str]) -> None:
    lines = []
    for i, (block, text) in enumerate(zip(blocks, texts), 1):
        text = text.strip()
        if not text:
            text = " "
        lines.append(str(i))
        lines.append(block.timing)
        lines.append(text)
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


# ── 句子级处理 ──

def split_sentences(text: str) -> List[str]:
    if not text or not text.strip():
        return [text] if text else []
    paragraphs = text.split("\n")
    all_sentences = []
    for para in paragraphs:
        para = para.strip()
        if not para:
            continue
        parts = re.split(r"(?<=[。！？])", para)
        for part in parts:
            if not part.strip():
                continue
            sents = _split_english_sentences(part.strip())
            all_sentences.extend(sents)
    merged = _merge_short_sentences(all_sentences)
    return merged if merged else [text]


def _split_english_sentences(text: str) -> List[str]:
    result = []
    current = ""
    i = 0
    while i < len(text):
        ch = text[i]
        current += ch
        if ch in ".!?" and (ch != "." or _is_sentence_end(text, i)):
            if i + 1 >= len(text) or text[i + 1].isspace():
                result.append(current.strip())
                current = ""
        i += 1
    if current.strip():
        result.append(current.strip())
    return result if result else [text.strip()]


def _is_sentence_end(text: str, pos: int) -> bool:
    if text[pos] != ".":
        return True
    before = text[:pos].strip()
    raw_word = re.split(r"[\s,;:]+", before)[-1].strip("()[]{}「」『』【】\"'")
    last_word = raw_word.lower()
    if last_word in _ABBREVIATIONS:
        return False
    if re.match(r"^[A-Z]\.?$", raw_word):
        return False
    return True


def _merge_short_sentences(sentences: List[str]) -> List[str]:
    if len(sentences) <= 1:
        return sentences
    result = []
    for s in sentences:
        s = s.strip()
        if not s:
            continue
        if result and _is_short_and_no_endmark(s):
            result[-1] = result[-1] + " " + s
        else:
            result.append(s)
    return result


def _is_short_and_no_endmark(s: str) -> bool:
    if len(s) <= 3:
        return not any(c in s for c in ".!?。！？")
    return False


def sentence_cache_key(sentence: str, model: str, is_bilingual: bool) -> str:
    mode = "bilingual" if is_bilingual else "chinese_only"
    normalized = sentence.strip().lower()
    payload = f"{model}\n{mode}\n{normalized}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


# ── CJK 字符检测（统一导出，供其他模块使用）──

def is_cjk(ch: str) -> bool:
    """检测单个字符是否为 CJK 字符（统一函数，避免重复定义）"""
    cp = ord(ch)
    return any([
        0x4E00 <= cp <= 0x9FFF,  # CJK 统一表意文字
        0x3400 <= cp <= 0x4DBF,  # CJK 扩展 A
        0x20000 <= cp <= 0x2A6DF,  # CJK 扩展 B
        0x2E80 <= cp <= 0x2EFF,  # CJK 部首补充
        0x3000 <= cp <= 0x303F,  # CJK 标点
        0xFF00 <= cp <= 0xFFEF,  # 全角形式
        0x3040 <= cp <= 0x309F,  # 平假名
        0x30A0 <= cp <= 0x30FF,  # 片假名
        0xAC00 <= cp <= 0xD7AF,  # 韩文
    ])


# ── 繁简转换 ──

# 缓存 opencc 转换器，避免重复初始化
_opencc_converter = None
_opencc_tried = False


def _get_opencc_converter():
    """获取 opencc 转换器（带缓存）"""
    global _opencc_converter, _opencc_tried
    if _opencc_tried:
        return _opencc_converter
    _opencc_tried = True
    try:
        import opencc
        _opencc_converter = opencc.OpenCC("t2s")
    except ImportError:
        logger.info("opencc 未安装，使用内置繁简转换表（转换质量有限）")
        _opencc_converter = None
    return _opencc_converter


def to_simplified(text: str) -> str:
    converter = _get_opencc_converter()
    if converter is not None:
        try:
            return converter.convert(text)
        except Exception as e:
            logger.warning("opencc 转换失败: %s，回退到内置表", e)
    return "".join(_SIMPLE_T2S.get(ch, ch) for ch in text)


# ── 字幕文件查找 ──

def find_existing_subtitle(video: Path) -> Optional[Path]:
    stem = safe_stem(video.name)
    for f in video.parent.iterdir():
        if f.suffix not in SUB_EXTS:
            continue
        f_stem = f.stem
        if "backup" in f_stem.lower() or "translated" in f_stem.lower() or "bak" in f_stem.lower():
            continue
        if f_stem == stem or f_stem.startswith(stem + "."):
            return f
    return None


def match_video_for_subtitle(subtitle: Path, work_dir: Path) -> Optional[Path]:
    stem = safe_stem(subtitle.name).split(".")[0]
    for ext in VIDEO_EXTS:
        for base_dir in (work_dir, subtitle.parent):
            candidate = base_dir / f"{stem}{ext}"
            if candidate.exists():
                return candidate
    return None


# ── 辅助函数 ──

def safe_stem(name: str) -> str:
    """安全获取文件名主干，防止路径遍历攻击并限制长度"""
    # 先使用 Path.stem 去掉路径和扩展名
    stem = Path(name).stem
    # 移除路径遍历字符和其他危险字符
    stem = stem.replace("..", "_")
    stem = stem.replace("/", "_").replace("\\", "_")
    stem = stem.replace(":", "_").replace("*", "_")
    stem = stem.replace("?", "_").replace('"', "_")
    stem = stem.replace("<", "_").replace(">", "_")
    stem = stem.replace("|", "_")
    # 限制长度
    if len(stem) > MAX_FILENAME_STEM:
        stem = stem[:MAX_FILENAME_STEM]
    # 去除首尾空白和点号（防止隐藏文件）
    stem = stem.strip().strip('.')
    if not stem:
        stem = "subtitle"
    return stem


def fmt_file_size(path: Path) -> str:
    try:
        size = path.stat().st_size
        if size >= 1024 ** 3:
            return f"[{size / 1024 ** 3:.1f} GB]"
        if size >= 1024 ** 2:
            return f"[{size / 1024 ** 2:.1f} MB]"
        if size >= 1024:
            return f"[{size / 1024:.0f} KB]"
        return f"[{size} B]"
    except OSError:
        return ""


def _file_icon(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in VIDEO_EXTS:
        return "\U0001F3AC"  # 🎬
    if ext in SUB_EXTS:
        return "\U0001F4DD"  # 📝
    return "\U0001F4C4"  # 📄


def fmt_job_display(path: Path) -> str:
    return f"{_file_icon(path)}  {path.name}  {fmt_file_size(path)}"


def has_chinese(text: str, source_lang: str = "") -> bool:
    """判断字幕块是否已有中文翻译

    source_lang: 可选的源语言提示（如 "ja"），辅助判定
    """

    if "\n" in text:
        parts = text.split("\n", 1)
        second = parts[1].strip()
        if not second:
            return False
        for ch in second:
            if 0x4E00 <= ord(ch) <= 0x9FFF:
                return True
        first = parts[0]
        for ch in first:
            if 0x4E00 <= ord(ch) <= 0x9FFF:
                return True
        return False

    # 单行文本：检查是否含假名或日专有汉字
    if source_lang and (source_lang.startswith("ja") or source_lang.startswith("jap")):
        return False
    has_kana = False
    has_cjk = False
    has_jp_specific = False
    for ch in text:
        cp = ord(ch)
        if 0x3040 <= cp <= 0x30FF:  # 平假名/片假名
            has_kana = True
        elif 0x4E00 <= cp <= 0x9FFF:  # CJK 汉字
            has_cjk = True
        if ch in _JAPANESE_SPECIFIC_KANJI:
            has_jp_specific = True

    if has_kana or has_jp_specific:
        return False
    return has_cjk


# ── 文件工具 ──

def find_tool(name: str, base_dir: Path) -> Optional[str]:
    p = base_dir / name
    return str(p) if p.exists() else None


# ── 进度映射与跨文件总进度 ──


def make_post_mapper(post: Callable, start: float, end: float) -> Callable:
    """包装 post，将子阶段内部 0-100 映射到目标区间 [start, end]"""
    span = end - start

    def wrapped(msg):
        if isinstance(msg, dict) and msg.get("type") == "progress":
            pct = msg.get("percent", 0)
            msg = {**msg, "percent": start + (pct / 100.0) * span}
        post(msg)

    return wrapped


_TRANSCRIBE_STAGES = {"提取音频", "加载模型", "读取字幕", "转写中"}
_SPLIT_STAGES = {"组织输出", "跳过", "完成"}


class OverallProgress:
    """跨文件的整体进度跟踪：当前第几个文件 + 整体完成比例 + 全部文件预计完成时间"""

    def __init__(self, total: int, transcribe_weight: float = 80.0):
        self.total = max(total, 1)
        self.transcribe_weight = max(0.0, min(100.0, transcribe_weight))
        self.translate_weight = 100.0 - self.transcribe_weight
        self._file_progress: list[float] = [0.0] * self.total
        self._file_weights: list[Optional[float]] = [None] * self.total
        self.current_idx = 0
        self.current_within = 0.0
        self.start_ts: Optional[float] = None

    def set_file_translation_only(self, idx: int) -> None:
        if 1 <= idx <= self.total:
            self._file_weights[idx - 1] = 0.0

    def start(self) -> None:
        self.start_ts = time.time()

    def tick(self, idx: int, pct: float, stage: str = "") -> float:
        file_idx = idx - 1
        tw = self._file_weights[file_idx] if self._file_weights[file_idx] is not None else self.transcribe_weight
        tlw = 100.0 - tw
        if stage in _TRANSCRIBE_STAGES:
            within = pct * tw / 100.0
        elif stage == "翻译":
            within = tw + pct * tlw / 100.0
        elif stage in _SPLIT_STAGES:
            within = 100.0
        else:
            within = min(100.0, pct)
        self._file_progress[file_idx] = within
        self.current_idx = idx
        self.current_within = within
        overall = sum(self._file_progress) / self.total
        return max(0.0, min(100.0, overall))

    def set_complete(self) -> None:
        self._file_progress = [100.0] * self.total
        self.current_idx = self.total
        self.current_within = 100.0

    def eta(self) -> Tuple[str, str]:
        if self.start_ts is None:
            return "--:--", "--:--"
        fraction = sum(self._file_progress) / 100.0 / self.total
        if fraction <= 0:
            return "--:--", "--:--"
        remain, finish = estimate_eta(self.start_ts, fraction)
        return remain, finish
