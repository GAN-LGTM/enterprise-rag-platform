"""
混合检索 —— 向量语义 + 全文关键词，RRF 倒数排名融合（需求书 FR-KB-03）。

RRF 公式： score = Σ 1 / (k + rank_i)，k=60（论文标准平滑常数）
为什么用 RRF 而不是分数相加：两路量纲不同（余弦 0~1 / ts_rank 无上界），
按排名融合不需要归一化，天然免疫"高分噪声压过另一路"的问题。

流水线：
    向量路 / 关键词路 → RRF 融合 → 可解释重排 → （追问时）范围收敛 → 相关性校验 → Top-K

其中重排与相关性校验见 retrieval/rerank.py 与 retrieval/quality.py，
均为纯规则实现，可用配置开关整体关闭，关闭后完全等价于原始 RRF。

降级链路：向量检索超时 → 自动降级为纯关键词，保证有结果返回。
"""
from __future__ import annotations

import asyncio
import time

import numpy as np

from ..config import settings
from ..logging_setup import get_logger
from ..metrics import NODE_LATENCY
from ..perm.policy import assert_dept_ids
from .quality import are_sources_relevant
from .rerank import boost_in_docs, rerank
from .store import Chunk, VectorStore

log = get_logger("retrieval")


def rrf_fuse(ranked_lists: list[list[Chunk]], k: int | None = None) -> list[Chunk]:
    """倒数排名融合。同一文档可能被两路同时召回，分数相加后排名更靠前。"""
    k = k or settings.RRF_K
    fused: dict[tuple, float] = {}
    holder: dict[tuple, Chunk] = {}

    for lst in ranked_lists:
        for rank, chunk in enumerate(lst, start=1):
            key = (chunk.department_id, chunk.doc_name, chunk.chunk_index)
            fused[key] = fused.get(key, 0.0) + 1.0 / (k + rank)
            holder.setdefault(key, chunk)

    out = []
    for key, score in sorted(fused.items(), key=lambda x: -x[1]):
        c = holder[key]
        out.append(Chunk(c.id, c.department_id, c.chunk_text, c.doc_name, c.page_num,
                         c.chunk_index, c.version, round(score, 6), c.metadata))
    return out


def confidence_of(score: float) -> str:
    """把融合分数映射成前端可读的置信度标签。"""
    if score >= 0.030:
        return "high"
    if score >= 0.016:
        return "medium"
    return "low"


async def hybrid_search(
    query: str,
    dept_ids: list[str],
    embedder,
    store: VectorStore,
    top_k: int | None = None,
    focus_docs: set[str] | None = None,
) -> tuple[list[Chunk], dict]:
    """
    返回 (融合后的 Top-K 片段, 检索诊断信息)。
    诊断信息用于 SSE 的 stage 事件，前端可以看到"检索了几条、用了哪几路"。

    参数 focus_docs：追问场景下上一轮**实际引用过**的文档名。
    传入后这些文档的片段会被优先召回，相当于把检索范围收敛到已确认相关的子集，
    既省上下文也提高精度（对应"上下文治理 Layer 2 引用溯源过滤"）。
    """
    dept_ids = assert_dept_ids(dept_ids)
    top_k = top_k or settings.TOP_K_FINAL
    diag: dict = {"dept_ids": dept_ids, "vector_hit": 0, "fts_hit": 0,
                  "vector_degraded": False, "cost_ms": 0.0,
                  "reranked": False, "relevant": True, "followup_scope": 0}

    if not dept_ids:
        return [], diag

    start = time.perf_counter()

    embedding: np.ndarray = await embedder.aembed(query)

    vector_hits: list[Chunk] = []
    try:
        vector_hits = await asyncio.wait_for(
            store.vector_search(embedding, dept_ids, settings.TOP_K_VECTOR),
            timeout=settings.VECTOR_TIMEOUT_MS / 1000,
        )
    except asyncio.TimeoutError:
        diag["vector_degraded"] = True
        log.warning("retrieval.vector_timeout", dept_ids=dept_ids)
    except Exception as e:  # noqa: BLE001
        diag["vector_degraded"] = True
        log.error("retrieval.vector_failed", error=str(e))

    fts_hits = await store.fts_search(query, dept_ids, settings.TOP_K_FTS)

    diag["vector_hit"] = len(vector_hits)
    diag["fts_hit"] = len(fts_hits)
    diag["cost_ms"] = round((time.perf_counter() - start) * 1000, 2)
    NODE_LATENCY.labels(node="retrieve").observe((time.perf_counter() - start))

    merged = rrf_fuse([vector_hits, fts_hits])

    # ---- 融合后重排：标题/长短语命中加权，高频噪声词降权（RRF 主序不变）----
    if settings.RERANK_ENABLED and merged:
        merged = rerank(query, merged)
        diag["reranked"] = True

    # ---- 追问收敛：上一轮引用过的文档优先 ----
    if focus_docs and settings.CTX_FOLLOWUP_SCOPE_ENABLED and merged:
        scoped = [c for c in merged if (c.doc_name or "") in focus_docs]
        diag["followup_scope"] = len(scoped)
        if scoped:
            merged = boost_in_docs(merged, set(focus_docs))
            # 范围足够时收窄候选数：少传上下文，也不让无关文档挤占 top_k
            top_k = min(top_k, max(3, len(scoped) + 1))

    merged = merged[:top_k]

    # ---- 幻觉治理防线一：来源相关性校验 ----
    # 只在"确实检索到了东西"时才校验；一个片段都没有由上层按"无结果"处理。
    if merged and settings.RELEVANCE_GATE_ENABLED:
        diag["relevant"] = are_sources_relevant(query, merged)

    diag["cost_ms"] = round((time.perf_counter() - start) * 1000, 2)
    log.info("retrieval.done", fused=len(merged), **diag)
    return merged, diag
