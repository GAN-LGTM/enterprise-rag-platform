"""
分块策略：递归分块 + 语义优先（对应需求书 FR-KB-02）。

原则：
  1. 先按段落/句子等自然边界切，切不动了才按字符硬切 —— 保证语义完整
  2. 相邻块保留 overlap，避免答案刚好被切断
  3. 表格块不切（切了表头就丢了列名），整块入库
"""
from __future__ import annotations

import re

from ..config import settings

# 自然边界：段落 → 中文句号/分号 → 换行
_SENT_SPLIT = re.compile(r"(?<=[。！？；!?;\n])")
_PARA_SPLIT = re.compile(r"\n\s*\n")


def chunk_text(text: str, size: int | None = None, overlap: int | None = None) -> list[str]:
    size = size or settings.CHUNK_SIZE
    overlap = overlap or settings.CHUNK_OVERLAP

    text = text.strip()
    if len(text) <= size:
        return [text] if text else []

    pieces: list[str] = []
    for para in _PARA_SPLIT.split(text):
        para = para.strip()
        if not para:
            continue
        if len(para) <= size:
            pieces.append(para)
        else:
            pieces.extend(_split_long(para, size))

    return _merge_with_overlap(pieces, size, overlap)


def _split_long(text: str, size: int) -> list[str]:
    out, buf = [], ""
    for sent in _SENT_SPLIT.split(text):
        if not sent:
            continue
        if len(buf) + len(sent) <= size:
            buf += sent
        else:
            if buf:
                out.append(buf)
            if len(sent) > size:  # 单句超长，按字符硬切
                out.extend(sent[i:i + size] for i in range(0, len(sent), size))
                buf = ""
            else:
                buf = sent
    if buf:
        out.append(buf)
    return out


def _merge_with_overlap(pieces: list[str], size: int, overlap: int) -> list[str]:
    """把短片段拼接成接近 size 的块，并在块之间带上 overlap 尾巴。"""
    merged: list[str] = []
    buf = ""
    for p in pieces:
        if not buf:
            buf = p
        elif len(buf) + len(p) + 1 <= size:
            buf = f"{buf}\n{p}"
        else:
            merged.append(buf)
            tail = buf[-overlap:] if overlap and len(buf) > overlap else ""
            buf = f"{tail}\n{p}" if tail else p
    if buf:
        merged.append(buf)
    return [m for m in merged if m.strip()]


def chunk_blocks(blocks) -> list[tuple[str, int | None]]:
    """
    输入 ParsedBlock 列表，输出 [(chunk_text, page_num), ...]。
    表格/类表格内容（含大量 ' | '）整块保留，不参与切分。
    """
    out: list[tuple[str, int | None]] = []
    for b in blocks:
        text = (b.text or "").strip()
        if not text:
            continue
        if text.count(" | ") >= 2:  # 表格行，整块入库
            out.append((text, b.page_num))
            continue
        for c in chunk_text(text):
            out.append((c, b.page_num))
    return out
