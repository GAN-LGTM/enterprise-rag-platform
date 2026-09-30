"""
LangGraph 的 5 个节点。

  classify_intent  → 四层递进意图识别
  permission_gate  → 6 条规则权限硬校验（检索前）
  retrieve         → Redis 缓存 → 混合检索（FTS + pgvector + RRF）
  generate         → 组装 Prompt，产出 token 流（SSE 的数据源）
  deny / error     → 友好拒答与异常兜底

每个节点都带了耗时埋点（node_timer），直接回答"慢在哪个节点"这个问题。
"""
from __future__ import annotations

import time
from collections.abc import AsyncIterator

from ..config import settings
from ..db.models import DEPARTMENTS, dept_name
from ..logging_setup import get_logger, node_timer
from ..llm.base import LLMMessage
from ..intent.chitchat import chitchat_reply, is_chitchat
from ..retrieval.context import build_context_plan, clip_history
from ..retrieval.hybrid import confidence_of, hybrid_search
from ..retrieval.quality import insufficient_answer
from ..schemas.dto import Citation
from .state import GraphState

log = get_logger("graph.nodes")

SYSTEM_PROMPT = """你是企业知识检索中台的智能助手。请严格遵守：
1. 只依据【参考资料】回答；资料里没有的信息，明确说明"未在知识库中检索到"，不要编造。
2. 回答使用中文，结构化表达，重要结论前置。
3. 涉及金额、日期、标准等数字信息，必须与原文一致。
4. 每个关键结论后用方括号标注依据来源编号（如 [1]），编号只能取自【参考资料】列出的序号，不要杜撰。
5. 不要透露你看到的部门 ID、权限规则等系统内部信息。"""

# 为什么必须显式要求标注编号（而不是靠模型自觉）：
# 生成后会按答案里实际出现的 [N] 过滤引用卡片（见 retrieval/quality.normalize_used_citations）。
# 模型不标编号，前端就只能把全部 8 条召回结果都当"依据"展示给用户——
# 其中大部分跟答案无关，反而消解了"答案溯源"本该建立的信任。

# 闲聊/操作类意图：不套知识库约束，自然对话即可
CHITCHAT_SYSTEM_PROMPT = """你是企业知识检索中台的智能助手「小知」。
知识问答之外，用户也可能和你日常闲聊、打招呼、问通用问题——这些请像正常助手一样
自然、友好地回答，可以适当口语化。回答使用中文，保持简洁。
注意：
- 同一轮对话里不要重复自我介绍（"我是企业知识检索中台的智能助手"整轮最多出现一次），
  也不要每次都用同一套开场白。
- 用户在致谢/告别/附和时，简短回应即可（如"不客气，有需要随时问我～"），
  不要再推销功能。
遇到公司制度、部门业务类问题时，建议用户直接提问，你会基于权限范围内的知识库作答。
不要透露部门 ID、权限规则等系统内部信息。"""


def build_nodes(container):
    cache = container.cache
    store = container.store
    embedder = container.embedder
    llm = container.llm
    classifier = container.classifier

    # ============================================================
    # 节点 1：意图识别
    # ============================================================
    async def classify_intent(state: GraphState) -> dict:
        with node_timer("classify_intent"):
            result = await classifier.classify(
                state["question"], state.get("last_intent"),
                trace_id=state.get("trace_id", "-"), user=state.get("user"),
            )
            return {
                "intent": result.intent,
                "intent_confidence": result.confidence,
                "intent_layer": result.layer,
                "intent_reason": result.reason,
                "intent_need_confirm": result.need_confirm,
                "intent_clarify": result.clarify,
                "target_depts": result.target_depts,
                "stage": "classify_intent",
            }

    # ============================================================
    # 节点 2：权限校验门（检索前硬拦截）
    # ============================================================
    async def permission_gate(state: GraphState) -> dict:
        with node_timer("permission_gate"):
            from ..perm.policy import check_permission

            decision = check_permission(
                state["user"], state.get("intent", "dept_kb"), state.get("target_depts")
            )
            log.info("perm.decision", allowed=decision.allowed, rule=decision.hit_rule,
                     dept_ids=decision.dept_ids, intent=state.get("intent"))
            return {
                "decision": decision,
                "dept_ids": decision.dept_ids,
                "stage": "permission_gate",
            }

    # ============================================================
    # 节点 3：检索（缓存 → 混合检索 → 质量校验）
    # ============================================================
    async def retrieve(state: GraphState) -> dict:
        with node_timer("retrieve"):
            from ..cache.cache import cache_key

            dept_ids = state.get("dept_ids", [])
            question = state["question"]
            key = cache_key(question, dept_ids)

            # ---- 上下文治理：先算出本轮策略（追问 / 收尾 / 裁剪后的历史）----
            # ctx_plan 会被 generate 节点复用：历史裁剪只需算一次，也不用在两个节点
            # 各写一套判断逻辑。
            ctx = _context_plan(question, state.get("history") or [])
            plan = {
                "ctx_plan": ctx,
                "focus_docs": ctx.get("focus_docs", set()),
                "low_evidence": False,
                "stage": "retrieve",
            }

            # 出公网脱敏词集：部门全名 + 用户身份（显示名/账号/所属部门）+ 内部术语。
            # 任何发往公网的查询都会先过 sanitize_query 剔除这些词，绝不携带内网上下文。
            redact: set[str] = {d.name for d in DEPARTMENTS.values()}
            u = state.get("user")
            if u is not None:
                redact.add(u.display_name)
                redact.add(u.username)
                redact.add(dept_name(u.dept_id))
            redact.update(settings.INTERNAL_TERMS)

            # 公网搜索总开关：OFFLINE_MODE=true（私有化默认）时关闭 → 零外网流量；
            # 即便开启，外部工具也只收"脱敏后的问题"，不碰内网文档/身份。
            from ..tools import realtime, websearch

            ext_ok = settings.PUBLIC_SEARCH_ENABLED

            # ⓪ 外部实时工具（天气 / 财经行情 …）：先在工具注册表里找能答的，
            #    拿到结构化实时数据后再让模型基于数据作答。
            #    注册表式设计（tools/realtime.TOOL_ROUTER）：新增工具只需注册一行，本节点零改动。
            #    命中工具的语义由各工具自己判定，这里只负责"该不该放行"——
            #      · dept_scoped（本轮圈定了部门知识库）时不轻易出网，除非命中强时效问题；
            #      · 出网前统一脱敏，公网只收到"问题本身"。
            if ext_ok:
                rt = await realtime.dispatch_realtime(
                    question, redact=redact, dept_scoped=bool(dept_ids), phase="primary"
                )
                if rt:
                    return {
                        **plan,
                        "cache_hit": False, "chunks": [], "citations": [],
                        "retrieval_diag": {"source": f"realtime_{rt.answer_source or rt.tool}",
                                           "cost_ms": 0.0},
                        "realtime_context": rt.text, "realtime_tool": rt.tool,
                        "answer_source": rt.answer_source or rt.tool,
                    }
                # 工具失败 / 无数据 → 静默降级，继续走原流程（LLM 会自然说明无法获取实时数据）

            # ⓪' 通用联网搜索：闲聊/操作类里带时效性的（dept_ids 为空才走），
            #     以及强时效问题（"今天金价""最新新闻"——时间词+行情名词，知识库必然没有，无视意图直接搜）
            if ext_ok and ((not dept_ids and websearch.is_search_query(question))
                           or websearch.is_hard_realtime(question)):
                rs = await websearch.fetch_search(question, redact=redact)
                if rs:
                    tool_name, ctx_text = rs
                    return {
                        **plan,
                        "cache_hit": False, "chunks": [], "citations": [],
                        "retrieval_diag": {"source": "websearch", "cost_ms": 0.0},
                        "realtime_context": ctx_text, "realtime_tool": tool_name,
                        "answer_source": "web",
                    }
            # ① 查缓存
            if state.get("cache_enabled", True):
                hit = await cache.get(key)
                if hit:
                    log.info("cache.hit", key=key[:16])
                    return {
                        **plan,
                        "cache_hit": True,
                        "chunks": [],
                        "citations": [Citation(**c) for c in hit.get("citations", [])],
                        "retrieval_diag": {"source": "cache", "cost_ms": 0.0},
                        "cached_answer": hit.get("answer", ""),
                        "answer_source": "kb" if hit.get("citations") else "chat",
                    }

            # ② 混合检索
            if not dept_ids:  # chitchat / operation 不需要检索
                # 但通用知识类问题（"XX是什么""XX是谁"）不能就这么丢给模型凭记忆答 ——
                # 那正是最容易一本正经编内容的地方。先让百科兜底，拿不到再退回闲聊。
                if ext_ok:
                    rt = await realtime.dispatch_realtime(
                        question, redact=redact, dept_scoped=False, phase="fallback"
                    )
                    if rt:
                        log.info("retrieve.chitchat_fallback", tool=rt.tool,
                                 question=question[:40])
                        return {
                            **plan,
                            "cache_hit": False, "chunks": [], "citations": [],
                            "retrieval_diag": {"source": f"realtime_{rt.answer_source or rt.tool}",
                                               "cost_ms": 0.0},
                            "realtime_context": rt.text, "realtime_tool": rt.tool,
                            "answer_source": rt.answer_source or rt.tool,
                        }
                return {**plan, "cache_hit": False, "chunks": [], "citations": [],
                        "retrieval_diag": {"source": "skip"},
                        "answer_source": "chat"}

            # 会话收尾语（"好的""不需要"）不再发起一轮全量检索：省一次向量查询，
            # 也避免把无关 behind-the-scenes 文档塞进本轮上下文。
            if settings.CTX_GOVERNANCE_ENABLED and ctx.get("boundary"):
                return {**plan, "cache_hit": False, "chunks": [], "citations": [],
                        "retrieval_diag": {"source": "boundary"},
                        "answer_source": "chat"}

            chunks, diag = await hybrid_search(
                question, dept_ids, embedder, store,
                focus_docs=plan["focus_docs"] if settings.CTX_GOVERNANCE_ENABLED else None,
            )
            citations = [
                Citation(
                    doc_name=c.doc_name, page_num=c.page_num, department_id=c.department_id,
                    department_name=dept_name(c.department_id), score=c.score,
                    confidence=confidence_of(c.score), snippet=c.chunk_text[:180],
                )
                for c in chunks
            ]
            # 幻觉治理防线一：有片段但都不相关 → 标记依据不足，
            # generate 节点据此下发确定性话术，而不是让模型拿着无关资料自由发挥。
            low_evidence = bool(chunks) and not diag.get("relevant", True)
            if low_evidence:
                log.warning("retrieval.low_evidence", question=question[:40], hits=len(chunks))

            # ②' 兜底工具（百科）：内网没检索到、或检索到了但都不相关时，
            #     才轮到公网百科。既不让百科盖过公司制度，也不让用户对着"未检索到"干瞪眼。
            if ext_ok and (not chunks or low_evidence):
                rt = await realtime.dispatch_realtime(
                    question, redact=redact, dept_scoped=bool(dept_ids), phase="fallback"
                )
                if rt:
                    log.info("retrieve.fallback_tool", tool=rt.tool,
                             question=question[:40])
                    return {
                        **plan,
                        "cache_hit": False, "chunks": [], "citations": [],
                        "retrieval_diag": {"source": f"realtime_{rt.answer_source or rt.tool}",
                                           "cost_ms": 0.0},
                        "realtime_context": rt.text, "realtime_tool": rt.tool,
                        "answer_source": rt.answer_source or rt.tool,
                    }
            log.info("retrieve.decision", relevant=diag.get("relevant"), chunks=len(chunks),
                     low_evidence=low_evidence, answer_source="kb", plan_keys=sorted(plan.keys()))

            # **plan 必须放最前：plan 里预置了 low_evidence=False / stage，
            # 若写在末尾会反向覆盖本分支刚算出的值（low_evidence 被恒置 False，
            # 防线二就彻底失效，模型会拿着无关文档硬答）。
            return {
                **plan,
                "cache_hit": False, "chunks": chunks, "citations": citations,
                "retrieval_diag": diag, "answer_source": "kb",
                "low_evidence": low_evidence,
            }

    # ============================================================
    # 节点 4：生成（产出 token 流）
    # ============================================================
    async def generate(state: GraphState) -> dict:
        with node_timer("generate"):
            question = state["question"]
            citations: list[Citation] = state.get("citations", [])
            history = state.get("history") or []
            ctx = state.get("ctx_plan") or {}

            # 滑动窗口：优先用 retrieve 阶段算好的裁剪结果（历史只裁剪一次），
            # 兜底才自己裁（例如 MiniGraph 下 state 传递顺序异常时）。
            history = ctx.get("clipped") if isinstance(ctx, dict) and ctx.get("clipped") is not None \
                else _clip_history(history)

            if state.get("cache_hit") and state.get("cached_answer"):
                stream = _pseudo_stream(state["cached_answer"])
                if state.get("intent_clarify"):
                    stream = _with_clarify(state["intent_clarify"], stream)
                return {"token_stream": stream, "stage": "generate",
                        "answer": state["cached_answer"],
                        "citations": state.get("citations", [])}

            # 幻觉治理防线二：检索到了片段但都不相关 → 确定性话术，不自由生成。
            # 此时模型手里的资料跟问题无关，它越"努力"回答，编造概率越高。
            if settings.RELEVANCE_GATE_ENABLED and state.get("low_evidence"):
                answer = insufficient_answer(question, citations)
                return {"token_stream": _pseudo_stream(answer), "stage": "generate",
                        "answer": answer, "citations": citations, "low_evidence": True}

            # 纯客套话（谢谢/再见/附和/问候）→ 确定性场景化回复，不过 LLM：
            # 毫秒级响应且天然不重复；也避免了模型把"好的谢谢"又答成一遍功能推销。
            # 上一轮是请假/报销流程时同样适用——"不客气～"本身就是对上轮的自然衔接。
            if (state.get("intent") == "chitchat" and not citations
                    and not state.get("realtime_context")
                    and _is_pure_courtesy(question)):
                answer = chitchat_reply(question)
                return {"token_stream": _pseudo_stream(answer), "stage": "generate",
                        "answer": answer, "citations": [], "answer_source": "chat"}

            messages = _build_messages(
                question, state.get("chunks", []), history, state.get("intent"),
                state.get("realtime_context"), state.get("realtime_tool"),
            )
            llm_meta: dict = {}
            stream = llm.astream(messages, meta=llm_meta)
            # 意图落在模糊地带（L3 置信度 0.5~0.8 / L4 安全降级）时，
            # 先在流的最前面补一句确认引导，让用户一句话就能纠偏，避免整段答案被误路由。
            if state.get("intent_clarify"):
                stream = _with_clarify(state["intent_clarify"], stream)
            # 显式带上 citations：LangGraph 节点间传递的是 state 副本，
            # 这里不回传的话，上层只能看到本节点的输出，拿不到 retrieve 阶段的引用列表。
            # llm_meta 记录实际应答的后端（流式消费过程中填充），供上层决定降级答案是否进缓存。
            return {"token_stream": stream, "stage": "generate",
                    "citations": state.get("citations", []), "llm_meta": llm_meta}

    # ============================================================
    # 节点 5：无权限拒答
    # ============================================================
    async def deny(state: GraphState) -> dict:
        with node_timer("deny"):
            decision = state.get("decision")
            msg = decision.user_message if decision else "抱歉，您没有访问该知识的权限。"

            async def _gen() -> AsyncIterator[str]:
                for ch in msg:
                    yield ch

            return {
                "token_stream": _gen(), "error_code": "PERMISSION_DENIED",
                "user_message": msg, "citations": [], "stage": "deny",
            }

    # ============================================================
    # 节点 6：错误兜底
    # ============================================================
    async def handle_error(state: GraphState) -> dict:
        with node_timer("handle_error"):
            msg = state.get("user_message") or "服务暂时不可用，请稍后重试。"
            log.error("graph.error_node", error=state.get("error"), code=state.get("error_code"))

            async def _gen() -> AsyncIterator[str]:
                for ch in msg:
                    yield ch

            return {"token_stream": _gen(), "stage": "handle_error"}

    return {
        "classify_intent": classify_intent,
        "permission_gate": permission_gate,
        "retrieve": retrieve,
        "generate": generate,
        "deny": deny,
        "handle_error": handle_error,
    }


# ------------------------------------------------------------
# 辅助函数
# ------------------------------------------------------------
def _is_pure_courtesy(question: str) -> bool:
    """是否为纯客套话（谢谢/再见/附和）。与意图分类器 L0 同一口径。"""
    return is_chitchat(question)


def _context_plan(question: str, history: list[dict]) -> dict:
    """上下文治理策略。开关关闭时返回"全量、不收敛"的等价默认策略。"""
    if not settings.CTX_GOVERNANCE_ENABLED:
        return {"followup": False, "boundary": False, "redirect": False,
                "focus_docs": set(), "clipped": history}
    return build_context_plan(question, history, max_chars=settings.CTX_HISTORY_MAX_CHARS)


def _clip_history(history: list[dict]) -> list[dict]:
    """历史裁剪的独立入口（ctx_plan 缺失时的兜底）。"""
    if not settings.CTX_GOVERNANCE_ENABLED:
        return history
    return clip_history(history, max_chars=settings.CTX_HISTORY_MAX_CHARS,
                        max_turns=settings.CTX_HISTORY_MAX_TURNS)
def _build_messages(
    question: str, chunks, history: list[dict], intent: str | None = None,
    realtime_context: str | None = None, realtime_tool: str | None = None,
) -> list[LLMMessage]:
    # 无检索结果的闲聊/操作类意图 → 自然对话人设，避免"未在知识库检索到"的答非所问；
    # 有实时数据时也用自然对话人设（数据已在 user 消息里给出，不需要 RAG 约束）
    is_chitchat = (not chunks and intent in ("chitchat", "operation")) or bool(realtime_context)
    msgs: list[LLMMessage] = [
        LLMMessage(role="system", content=CHITCHAT_SYSTEM_PROMPT if is_chitchat else SYSTEM_PROMPT)
    ]

    # 多轮历史：入参已是按字符预算裁剪过的滑动窗口（见 retrieval/context.clip_history）。
    # 这里只留一层条数上限的安全阀：正常情况下它不会再触发裁剪。
    for turn in history[-(settings.CTX_HISTORY_MAX_TURNS * 2):]:
        msgs.append(LLMMessage(role=turn.get("role", "user"), content=turn.get("content", "")))

    if chunks:
        ref_lines = []
        for i, c in enumerate(chunks, 1):
            ref_lines.append(
                f"[{i}] 来源：{c.doc_name}"
                f"{f' 第{c.page_num}页' if c.page_num else ''}"
                f"（{dept_name(c.department_id)}）\n{c.chunk_text}"
            )
        user = (
            f"【参考资料】\n" + "\n\n".join(ref_lines) + "\n\n"
            f"【用户问题】{question}\n\n"
            "请给出简洁准确的回答，并在每个关键结论后用方括号标注所依据的资料编号（如 [1]）。"
        )
    elif realtime_context:
        if realtime_tool == "websearch":
            user = (
                f"【实时数据（刚刚通过网络搜索获得）】\n{realtime_context}\n\n"
                f"【用户问题】{question}\n\n"
                "请综合以上搜索结果和网页摘录自然地回答。数字（价格、日期、比分等）必须来自以上材料，"
                "不要编造；不同来源数据不一致时以更新时间新的为准并简要说明；回答末尾注明信息来源网站。"
                "并在回答末尾明确标注：「以上信息来自公开互联网，仅供参考，不代表公司内部信息。」"
            )
        else:
            user = (
                f"【实时数据（由外部工具刚查询得到）】\n{realtime_context}\n\n"
                f"【用户问题】{question}\n\n"
                "请基于以上实时数据自然、简洁地回答。数据中的数字（温度、日期、概率等）必须原样使用，不要编造。"
            )
    else:
        if is_chitchat:
            user = question
        else:
            user = f"【用户问题】{question}\n（知识库中没有检索到相关资料，请礼貌说明并给出通用建议）"

    msgs.append(LLMMessage(role="user", content=user))
    return msgs


async def _with_clarify(clarify: str, stream) -> AsyncIterator[str]:
    """在答案流最前面插入一句确认引导（意图模糊地带用），随后原样透传。"""
    yield clarify + "\n\n"
    async for ch in stream:
        yield ch


async def _pseudo_stream(text: str) -> AsyncIterator[str]:
    """
    缓存命中时也要"流式"返回。
    原因：前端体验必须一致——用户看到的永远是逐字输出，
    而不是有时秒回、有时逐字。代价只是一点 CPU。
    """
    import asyncio

    delay = settings.STREAM_CACHE_CHUNK_DELAY
    for ch in text:
        yield ch
        if delay > 0:
            await asyncio.sleep(delay)
