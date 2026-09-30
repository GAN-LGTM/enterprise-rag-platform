"""
文档入库流水线：解析 → 分块 → 去重 → 向量化 → 入库 → 失效缓存。

一致性保障（面试常问"文档更新后索引怎么同步"）：
  1. 先删旧向量，再写新向量（同一 doc_name + department 维度）
  2. 内容哈希去重：内容没变就不重复入库
  3. 入库成功后主动清除该部门的全部问答缓存，避免用户读到旧知识的答案
"""
from __future__ import annotations

import hashlib
import os
import re
import uuid

from ..config import settings
from ..logging_setup import get_logger, node_timer
from ..retrieval.store import Chunk
from .chunker import chunk_blocks
from .parser import ocr_pdf, parse_file
from .version_store import current_version, next_version, record_version

log = get_logger("ingest")

# 入库前脱敏：身份证 / 手机号 / 邮箱 / 银行卡。匹配到即替换为占位符，
# 保证向量与检索存储中不留原始 PII（NFR-SEC-09）。
_MASK_PATTERNS = [
    (re.compile(r"\b\d{17}[\dXx]\b"), "身份证"),
    (re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"), "手机号"),
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "邮箱"),
    (re.compile(r"(?<!\d)\d{15,19}(?!\d)"), "银行卡"),
]


def mask_sensitive(text: str) -> str:
    if not settings.MASK_SENSITIVE_DATA or not text:
        return text
    t = text
    for pat, label in _MASK_PATTERNS:
        t = pat.sub(lambda m, lab=label: f"[{lab}已脱敏]", t)
    return t

# 内容哈希（去重）
_DOC_HASH: dict[tuple[str, str], str] = {}


async def ingest_document(
    file_path: str,
    filename: str,
    department_id: str,
    uploaded_by: str,
    container,
    change_note: str = "",
) -> dict:
    with node_timer("ingest"):
        os.makedirs(settings.DOC_UPLOAD_DIR, exist_ok=True)

        # ① 解析（扫描件走 OCR）
        try:
            blocks = parse_file(file_path, filename)
        except RuntimeError as e:
            if settings.OCR_ENABLED and filename.lower().endswith(".pdf"):
                log.info("ingest.fallback_ocr", filename=filename)
                blocks = ocr_pdf(file_path)
            else:
                raise

        # ② 分块
        pairs = chunk_blocks(blocks)
        if not pairs:
            raise ValueError("文档解析后没有有效文本内容")

        # ②.5 脱敏：在向量化之前抹掉文本中的 PII（身份证 / 手机号 / 邮箱 / 银行卡）
        pairs = [(mask_sensitive(t), pg) for t, pg in pairs]

        # ③ 内容哈希去重
        joined = "".join(t for t, _ in pairs)
        content_hash = hashlib.md5(joined.encode("utf-8")).hexdigest()
        hkey = (filename, department_id)
        if _DOC_HASH.get(hkey) == content_hash:
            log.info("ingest.duplicate_skipped", filename=filename, dept=department_id)
            return {
                "doc_name": filename, "department_id": department_id,
                "chunk_count": 0, "version": current_version(filename, department_id),
                "task_id": "-", "skipped": True,
            }

        version = next_version(filename, department_id)

        # ④ 先删旧版本，再写新版本（保证不会新旧混杂）
        if version > 1:
            await container.store.delete_doc(filename, department_id)

        chunks = [
            Chunk(id=None, department_id=department_id, chunk_text=t, doc_name=filename,
                  page_num=pg, chunk_index=i, version=version,
                  metadata={"uploaded_by": uploaded_by, "hash": content_hash})
            for i, (t, pg) in enumerate(pairs)
        ]
        embeddings = [container.embedder.embed_sync(c.chunk_text) for c in chunks]
        await container.store.add_chunks(chunks, embeddings)
        _DOC_HASH[hkey] = content_hash

        # 登记版本历史（FR-KB-05）
        record_version(filename, department_id, version, len(chunks), uploaded_by,
                       file_path, content_hash, change_note)

        # ⑤ 失效该部门问答缓存
        await container.cache.invalidate_department(department_id)

        log.info("ingest.done", filename=filename, dept=department_id,
                 chunks=len(chunks), version=version)
        return {
            "doc_name": filename, "department_id": department_id,
            "chunk_count": len(chunks), "version": version,
            "task_id": uuid.uuid4().hex[:12], "skipped": False,
        }
