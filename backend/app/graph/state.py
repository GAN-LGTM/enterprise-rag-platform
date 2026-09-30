"""
LangGraph 工作流状态定义。

State 里除了业务字段，还承载两个"工程向"字段：
  token_stream : 异步生成器（LLM 的 token 流）。把它放进 State，
                 让"图编排"和"流式输出"两件事能共存——图负责决策，流负责吐字。
  timings      : 每个节点的耗时，直接支撑"节点级可观测性"。
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, TypedDict

from ..db.models import User
from ..perm.policy import PermissionDecision
from ..retrieval.store import Chunk
from ..schemas.dto import Citation


class GraphState(TypedDict, total=False):
    # ---- 上下文 ----
    trace_id: str
    user: User
    question: str
    session_id: str
    history: list[dict]
    last_intent: str | None

    # ---- 意图 ----
    intent: str
    intent_confidence: float
    intent_layer: str
    intent_reason: str
    intent_need_confirm: bool   # 意图落在模糊地带 → 生成时先追加确认引导
    intent_clarify: str
    target_depts: list[str]

    # ---- 权限 ----
    decision: PermissionDecision
    dept_ids: list[str]

    # ---- 检索 ----
    chunks: list[Chunk]
    retrieval_diag: dict
    cache_hit: bool
    cache_enabled: bool
    cached_answer: str
    # kb / web / weather / chat。LangGraph 只回传本类已声明的字段：
    # 漏声明会被静默丢弃，导致前端来源徽章恒为"纯闲聊"、知识缺口台账永不入库。
    answer_source: str

    # ---- 上下文治理（追问 / 会话边界 / 历史裁剪）----
    ctx_plan: dict          # {"followup":bool,"boundary":bool,"focus_docs":set,"clipped":[...]}
    focus_docs: set         # 追问时优先命中的文档名（上一轮实际引用过的）
    low_evidence: bool      # 检索结果相关性不足 → 走确定性话术而非自由生成

    # ---- 实时工具（天气等外部实时数据，存在时生成节点优先使用）----
    realtime_context: str
    realtime_tool: str

    # ---- 生成 ----
    citations: list[Citation]
    answer: str
    token_stream: AsyncIterator[str] | None
    # 实际应答的 LLM 后端信息（{"backend": "cloud:xxx" | "mock:mock-stream"}），
    # 流式消费过程中由 LLMManager 填充；上层据此决定降级答案是否写缓存。
    llm_meta: dict

    # ---- 控制 ----
    stage: str
    error: str
    error_code: str
    user_message: str
    timings: dict[str, float]
    extra: dict[str, Any]
