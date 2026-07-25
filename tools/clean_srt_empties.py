#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
清理 SRT 字幕文件中的空文本条目（无内容的字幕行）。
用法：python clean_srt_empties.py <文件或目录>
支持通配符：python clean_srt_empties.py *.srt
"""

import re
import sys
from pathlib import Path


def clean_srt(path: Path, inplace: bool = True) -> str:
    """读取 SRT 文件，移除空文本条目，返回清洗后的内容。"""
    text = path.read_text(encoding="utf-8-sig")

    # SRT block 解析
    pattern = re.compile(
        r"(?:(\d+)\s*(?:\r?\n|\r))?"  # 序号
        r"(\d+:\d{1,2}:\d{1,2}[,.]\d{1,3})\s*-->\s*(\d+:\d{1,2}:\d{1,2}[,.]\d{1,3})\s*(?:\r?\n|\r)"  # 时间戳
        r"((?:(?!(?:\r?\n|\r){2}).)+)",  # 文本
        re.DOTALL,
    )

    blocks = []
    for m in pattern.finditer(text):
        content = m.group(4).strip()
        if content:  # 只保留有内容的条目
            blocks.append(m.group(0).rstrip())

    result = "\n\n".join(blocks) + "\n"

    # 重编号
    lines = result.split("\n")
    new_lines = []
    idx = 0
    i = 0
    while i < len(lines):
        line = lines[i]
        # 检查是否是序号行（下一行是时间戳行）
        if i + 2 < len(lines) and "-->" in lines[i + 1]:
            idx += 1
            new_lines.append(str(idx))
            new_lines.append(lines[i + 1])
            # 文本行可能有多行（直到空行）
            j = i + 2
            text_parts = []
            while j < len(lines) and lines[j].strip():
                text_parts.append(lines[j])
                j += 1
            new_lines.extend(text_parts)
            new_lines.append("")
            i = j + 1 if j < len(lines) else j
        else:
            new_lines.append(line)
            i += 1

    cleaned = "\n".join(new_lines)

    if inplace:
        path.write_text(cleaned, encoding="utf-8")
        return f"✅ {path.name}: 已清理并重编号"
    return cleaned


def main():
    if len(sys.argv) < 2:
        print("用法: python clean_srt_empties.py <文件.srt 或 目录>")
        print("示例: python clean_srt_empties.py 字幕.srt")
        print("      python clean_srt_empties.py *.srt")
        sys.exit(1)

    targets = []
    for arg in sys.argv[1:]:
        p = Path(arg)
        if p.exists():
            if p.is_file() and p.suffix == ".srt":
                targets.append(p)
            elif p.is_dir():
                targets.extend(sorted(p.glob("*.srt")))
        else:
            # 尝试通配符
            from glob import glob
            for g in glob(arg):
                gp = Path(g)
                if gp.suffix == ".srt":
                    targets.append(gp)

    if not targets:
        print("❌ 未找到 .srt 文件")
        sys.exit(1)

    for t in targets:
        print(clean_srt(t, inplace=True))


if __name__ == "__main__":
    main()
