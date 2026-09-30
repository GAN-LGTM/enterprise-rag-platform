"""
问答接口 —— 核心是 POST /api/chat/stream 的 SSE 流式输出。

一帧一帧看：
    meta     立即返回 trace_id + 意图预判，前端可马上显示"正在检索…"
    stage    LangGraph 每个节点完成推一次，前端进度条走起来
    citation 检索完成即推引用卡片（不等正文），用户先看到"依据来自哪"
    token    逐字增量，前端打字机效果
    done     结束帧，带完整答案、节点耗时、是否命中缓存

工程细节：
  · 心跳帧：超过 15s 无数据自动补 ": ping"，防止 Nginx / 网关掐断长连接
  · 缓存命中同样走流式（伪流式），保证用户体验一致
  · 每个请求一个 trace_id，贯穿日志与 done 帧，报障时一搜定位
"""
from __future__ import annotations

import asyncio
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from ..config import settings
from ..core import audit, feedback_store, gap_store, ratelimit
from ..db.models import dept_name
from ..errors import RateLimited, SensitiveHit
from ..graph.state import GraphState
from ..logging_setup import bind_trace, get_logger
from ..metrics import LATENCY, REQUESTS, STREAM_ACTIVE
from ..retrieval.quality import normalize_used_citations
from ..schemas.dto import ChatStreamRequest, FeedbackRequest
from ..tools import realtime
from .. import session_store as store
from ..leave import leave_store as lstore
from ..leave import flow as leave_flow
from ..reimburse import reimburse_store as rbstore
from ..reimburse import flow as rb_flow
from .deps import active_user, current_user

# 走公网工具得到的答案来源。这些答案一律**不写进内网 RAG 缓存** ——
# 否则一条"今天的天气/行情"会被缓存下来，在 TTL 内被当成公司知识反复下发。
EXTERNAL_SOURCES = frozenset({"web", "weather", "market", "encyclopedia", "holiday"})
from .sse import CITATION, DONE, ERROR, HEARTBEAT, META, STAGE, STREAM_HEADERS, TOKEN, sse_frame

router = APIRouter(prefix="/api/chat", tags=["智能问答"])
log = get_logger("api.chat")

def _check_sensitive(q: str):
    for w in settings.SENSITIVE_WORDS:
        if w in q:
            raise SensitiveHit(detail={"word": w})


# ============================================================
#  ★ SSE 流式问答
# ============================================================
@router.post("/stream", summary="SSE 流式问答")
async def chat_stream(body: ChatStreamRequest, request: Request, user=Depends(active_user)):
    container = request.app.state.container
    _check_sensitive(body.question)

    # 每用户滑动窗口限流：防刷/防滥用（企业级基线），超限 429 + 建议等待秒数
    allowed, retry_after = ratelimit.check(user.user_id)
    if not allowed:
        raise RateLimited(f"提问太频繁啦，请 {retry_after} 秒后再试", retry_after=retry_after)

    # 会话归属：session_id 不存在或不属于当前用户时，为其新建归属会话，
    # 绝不读取他人的上下文历史（防横向越权）
    session_id = body.session_id or uuid.uuid4().hex[:12]
    session = store.get_session(user.user_id, session_id)
    if session is None:
        session = store.create_session(user.user_id, session_id)
    history = [{"role": m["role"], "content": m["content"]}
               for m in session["messages"][-20:]]
    trace_id = bind_trace(user_id=user.user_id, dept_ids=[])
    feedback_store.record_hot(body.question, user.dept_id)

    # ---- 请假 / 报销 工作流拦截：优先于普通 RAG ----
    # 读取并清除该用户的未读审批结果通知（审批通过/驳回），随本次响应一并返回前端弹提示。
    leave_events = lstore.pop_notifications(user.user_id)
    reimburse_events = rbstore.pop_notifications(user.user_id)
    leave_draft = lstore.get_draft(user.user_id)
    reimburse_draft = rbstore.get_draft(user.user_id)

    q = body.question

    # 1) 进度查询（最高优先级，避免"报销进度怎么样"误开新流程）
    if leave_flow.is_progress_query(q) and leave_draft is None:
        return await _leave_progress_stream(user, session_id, trace_id,
                                            leave_events, container)
    if rb_flow.is_progress_query(q) and reimburse_draft is None:
        return await _reimburse_progress_stream(user, session_id, trace_id,
                                                reimburse_events, container)

    # 2) 进行中的草稿优先续答（谁有草稿走谁）
    if reimburse_draft is not None:
        return await _reimburse_stream(user, q, session_id, trace_id,
                                      reimburse_events, container)
    if leave_draft is not None:
        return await _leave_stream(user, q, session_id, trace_id,
                                  leave_events, container)

    # 3) 新流程触发
    if rb_flow.is_trigger(q):
        return await _reimburse_stream(user, q, session_id, trace_id,
                                      reimburse_events, container)
    if leave_flow.is_trigger(q):
        return await _leave_stream(user, q, session_id, trace_id,
                                  leave_events, container)

    state: GraphState = {
        "trace_id": trace_id,
        "user": user,
        "question": body.question,
        "session_id": session_id,
        "history": history,
        "last_intent": None,
        "cache_enabled": body.use_cache,
        "timings": {},
    }

    async def event_generator():
        queue: asyncio.Queue = asyncio.Queue()
        task = asyncio.create_task(_producer(queue, state, body, user, session, session_id,
                                              container, leave_events, reimburse_events))
        STREAM_ACTIVE.inc()
        started = time.perf_counter()
        try:
            await queue.put(sse_frame(META, {
                "trace_id": trace_id, "session_id": session_id,
                "user": user.display_name, "dept": dept_name(user.dept_id),
                "store_mode": container.store_mode, "graph_engine": container.workflow.engine,
                "ts": time.time(),
            }))
            while True:
                try:
                    frame = await asyncio.wait_for(queue.get(), timeout=settings.STREAM_HEARTBEAT_SECONDS)
                except asyncio.TimeoutError:
                    yield HEARTBEAT          # 心跳，保活长连接
                    continue
                if frame is None:
                    break
                yield frame
            await task
        finally:
            STREAM_ACTIVE.dec()
            # 请求耗时埋点：rag_request_latency_seconds 此前只定义、从未 observe，
            # 导致运维手册里的 P95 耗时告警在任何情况下都不会触发（永远没有样本）。
            LATENCY.labels(endpoint="/api/chat/stream").observe(time.perf_counter() - started)
            if not task.done():
                task.cancel()

    return StreamingResponse(event_generator(), headers=STREAM_HEADERS)


async def _producer(
    queue: asyncio.Queue,
    state: GraphState,
    body: ChatStreamRequest,
    user,
    session: dict,
    session_id: str,
    container,
    leave_events: list,
    reimburse_events: list,
) -> None:
    """SSE 流式问答的产出协程。被提取为模块级函数以便单元测试中断行为。"""
    answer_parts: list[str] = []
    citations: list[dict] = []
    cache_hit = False
    final_intent = "dept_kb"
    dept_ids: list[str] = []
    answer_source = "chat"      # kb=内网知识库 / web=公网搜索 / weather=天气 / market=行情
                                # encyclopedia=百科 / holiday=节假日 / chat=纯闲聊
    trace_id = state["trace_id"]

    interrupted = False
    stream_failed = False
    try:
        async for kind, a, b in container.workflow.run_stream(state):
            if kind == "stage":
                node, out = a, b or {}
                payload = {"node": node, "label": _stage_label(node), "trace_id": trace_id}
                if node == "classify_intent":
                    final_intent = out.get("intent", "dept_kb")
                    payload.update({
                        "intent": final_intent,
                        "confidence": out.get("intent_confidence"),
                        "layer": out.get("intent_layer"),
                    })
                if node == "permission_gate":
                    dec = out.get("decision")
                    dept_ids = out.get("dept_ids", [])
                    if dec is not None:
                        payload.update({"allowed": dec.allowed, "rule": dec.hit_rule,
                                        "dept_ids": dept_ids})
                if node == "retrieve":
                    diag = out.get("retrieval_diag", {})
                    cache_hit = bool(out.get("cache_hit"))
                    if out.get("answer_source"):
                        answer_source = out["answer_source"]
                    payload.update({"cache_hit": cache_hit, "vector_hit": diag.get("vector_hit"),
                                    "fts_hit": diag.get("fts_hit"), "cost_ms": diag.get("cost_ms")})
                    # 走了外部工具时把进度文案换成真实路径，用户能看到"联网搜索了/查行情了"
                    src = diag.get("source") or ""
                    if src == "websearch":
                        payload["label"] = "联网搜索"
                    elif src.startswith("realtime_"):
                        # 工具名 → 文案由工具注册表提供，新增工具不需要改这里
                        payload["label"] = realtime.label_for(src[len("realtime_"):]) or "查询实时数据"
                await queue.put(sse_frame(STAGE, payload))

                if node in ("generate", "deny", "handle_error"):
                    cites = out.get("citations") or state.get("citations") or []
                    if cites:
                        citations = [c.model_dump() for c in cites]
                        await queue.put(sse_frame(CITATION, {"citations": citations}))
                    if out.get("error"):
                        await queue.put(sse_frame(ERROR, {
                            "code": out.get("error_code", "ERROR"),
                            "message": out.get("user_message", "处理失败"),
                        }))
            else:
                answer_parts.append(a)
                await queue.put(sse_frame(TOKEN, {"delta": a}))

    except asyncio.CancelledError:
        interrupted = True
        log.info("chat.interrupted", trace_id=trace_id, chars=len("".join(answer_parts)))
        raise
    except Exception as e:  # noqa: BLE001
        log.error("chat.stream_error", trace_id=trace_id, error=str(e))
        await queue.put(sse_frame(ERROR, {"code": "STREAM_ERROR", "message": f"{type(e).__name__}: {e}"}))
    finally:
        answer = "".join(answer_parts)
        timings = state.get("timings", {})

        # ---- 幻觉治理防线三：引用归一化 ----
        # 只保留答案里实际引用过的 [N]，并按出现顺序把编号重排成 1..N。
        # 未被引用的来源不再下发给前端，也不参与下一轮上下文，避免"看起来有依据其实没用"。
        if settings.CITATION_NORMALIZE_ENABLED and answer and citations:
            try:
                answer, citations = normalize_used_citations(answer, citations)
            except Exception as e:  # noqa: BLE001  归一化失败不能拖垮主流程
                log.warning("chat.citation_normalize_failed", error=str(e))

        # ---- 知识缺口沉淀 ----
        # 内问答不上来的（零结果 / 相关性不足）按部门归类计数，供管理端看"该补哪份文档"。
        if answer_source == "kb" and body.question.strip():
            if not citations:
                reason = "no_hit"
            elif state.get("low_evidence"):
                reason = "low_evidence"
            else:
                reason = ""
            if reason:
                gap_store.record_gap(body.question, user.dept_id, reason=reason,
                                     user_name=user.display_name, trace_id=trace_id)

        # 用户点击“停止”后前端会 abort SSE，event_generator 会 cancel 本任务。
        # 此时不能把半成品答案写进缓存或会话历史，否则下次问同样/类似问题会命中
        # 半截回答或带着半成品上下文，造成“接着上次说”的错觉。
        if interrupted:
            log.info("chat.interrupted_cleanup", trace_id=trace_id, chars=len(answer))

        if not interrupted:
            # 写回缓存（仅成功答案且未命中缓存时；公网/天气等外部答案不进内网 RAG 缓存，避免陈旧外网信息被当成内部知识反复下发）
            # 降级答案不进缓存：主模型故障时备用（mock）模型产出的答案质量低于主模型，
            # 缓存它会让 TTL 内所有相同提问都拿到降级答案——即使主模型已恢复。
            served_by = (state.get("llm_meta") or {}).get("backend")
            degraded = bool(served_by) and container.llm.primary_key != served_by
            if degraded:
                log.info("chat.cache_skip_degraded", served_by=served_by, trace_id=trace_id)
            if (answer and not cache_hit and not state.get("error") and body.use_cache
                    and answer_source not in EXTERNAL_SOURCES and not degraded):
                try:
                    from ..cache.cache import cache_key

                    key = cache_key(body.question, dept_ids or state.get("dept_ids", []))
                    await container.cache.set(
                        key, {"answer": answer, "citations": citations}, settings.CACHE_TTL_SECONDS
                    )
                    await container.cache.index_key(user.dept_id, key)
                except Exception as e:  # noqa: BLE001
                    log.warning("chat.cache_write_failed", error=str(e))

            # 更新会话历史（按账号隔离落盘；首轮问答自动命名会话）
            session_title = session["title"]
            if not state.get("error") and answer:
                try:
                    st = store.append_exchange(user.user_id, session_id,
                                               body.question, answer, citations)
                    session_title = (st or {}).get("title", session_title)
                except Exception as e:  # noqa: BLE001
                    log.warning("chat.session_write_failed", error=str(e))

            await queue.put(sse_frame(DONE, {
                "trace_id": trace_id, "session_id": session_id, "answer": answer,
                "citations": citations, "intent": state.get("intent", final_intent),
                "answer_source": answer_source,
                "cache_hit": cache_hit, "timings": timings,
                # 相关性不足时为 true：答案已被替换为确定性兜底话术，前端据此给出提示条，
                # 避免用户把它当成"有依据的正式答复"。
                "low_evidence": bool(state.get("low_evidence")),
                "dept_ids": dept_ids or state.get("dept_ids", []),
                "store_mode": container.store_mode,
                "graph_engine": container.workflow.engine,
                "leave_notifications": leave_events,
                "reimburse_notifications": reimburse_events,
            }))

        # 审计（JSON 落盘，重启不丢；管理端可按类型/用户检索）—— 中断也记录提问
        audit.record("chat", user_id=user.user_id, user_name=user.display_name,
                     detail=body.question, trace_id=trace_id,
                     intent=state.get("intent", final_intent),
                     dept_ids=dept_ids, session_id=session_id)

        # 指标：失败请求同样要计数 —— 否则 ApiErrorRateHigh 告警（按 status!="ok" 计算）
        # 的分子恒为 0，永远不可触发，告警规则形同虚设。
        req_status = "error" if (state.get("error") or stream_failed) else "ok"
        REQUESTS.labels(endpoint="/api/chat/stream", status=req_status).inc()
        await queue.put(None)


# ============================================================
#  引导式工作流（请假 / 报销）的统一 SSE 输出
# ============================================================
# 请假与报销的 SSE 逻辑此前是四个几乎逐字重复的函数（流程 + 进度 × 两个领域），
# 改一处要改四遍。这里收敛成一个参数化实现，领域差异全部通过 spec 表注入。

FLOW_SPECS = {
    "leave": {
        "flow": leave_flow, "flow_store": lstore, "intent": "leave",
        "node": "leave", "start_label": "请假流程", "progress_label": "查询请假进度",
        "notify_key": "leave_notifications",
        "empty_hint": "暂无请假记录，想说「我想请假」就可以发起申请～",
        "progress_title": "请假进度查询",
    },
    "reimburse": {
        "flow": rb_flow, "flow_store": rbstore, "intent": "reimburse",
        "node": "reimburse", "start_label": "报销流程", "progress_label": "查询报销进度",
        "notify_key": "reimburse_notifications",
        "empty_hint": "暂无报销记录，想说「我想报销」就可以发起申请～",
        "progress_title": "报销进度查询",
    },
}


async def _guided_flow_stream(user, question: str, session_id: str, trace_id: str,
                              events: list, container, *, kind: str,
                              start: bool) -> StreamingResponse:
    """
    引导式工作流的 SSE 输出：与 RAG 共用同一套前端事件解析（meta/stage/token/done），
    所以聊天界面里请假/报销问答与普通问答渲染完全一致，前端不需要写特殊分支。

    start=True  流程问答：无草稿则发起新流程，有草稿则把本条消息当作上一问的回答继续推进
    start=False 进度查询：只读当前此人的申请状态
    """
    spec = FLOW_SPECS[kind]
    flow_mod = spec["flow"]
    fstore = spec["flow_store"]

    # 确保会话存在（引导问答也要落盘，刷新后可恢复上下文）
    if store.get_session(user.user_id, session_id) is None:
        store.create_session(user.user_id, session_id)

    if start:
        draft = fstore.get_draft(user.user_id)
        if draft is None:
            _, answer = flow_mod.start(user)
        else:
            answer, _ = flow_mod.handle(user, question, draft)
        title = question
        engine = f"{kind}-flow"
    else:
        answer = flow_mod.query_progress(user) or spec["empty_hint"]
        title = spec["progress_title"]
        engine = f"{kind}-progress"

    try:
        store.append_exchange(user.user_id, session_id, title, answer, [])
    except Exception as e:  # noqa: BLE001
        log.warning("flow.session_write_failed", kind=kind, error=str(e))

    label = spec["start_label"] if start else spec["progress_label"]
    store_mode = getattr(container, "store_mode", "memory")

    async def gen():
        yield sse_frame(META, {
            "trace_id": trace_id, "session_id": session_id,
            "user": user.display_name, "dept": dept_name(user.dept_id),
            "store_mode": store_mode, "graph_engine": engine, "ts": time.time(),
        })
        yield sse_frame(STAGE, {"node": spec["node"], "label": label, "trace_id": trace_id})
        # 按行推送，前端 markdown 渲染更自然，也有轻微打字机效果
        for line in answer.split("\n"):
            yield sse_frame(TOKEN, {"delta": line + "\n"})
        yield sse_frame(DONE, {
            "trace_id": trace_id, "session_id": session_id, "answer": answer,
            "intent": spec["intent"], "citations": [], "cache_hit": False,
            "timings": {kind: 0}, spec["notify_key"]: events,
            "graph_engine": engine, "store_mode": store_mode,
        })

    return StreamingResponse(gen(), headers=STREAM_HEADERS)


async def _leave_stream(user, question: str, session_id: str, trace_id: str,
                        leave_events: list, container) -> StreamingResponse:
    """请假引导式对话。"""
    return await _guided_flow_stream(user, question, session_id, trace_id,
                                     leave_events, container, kind="leave", start=True)


async def _leave_progress_stream(user, session_id: str, trace_id: str,
                                 leave_events: list, container) -> StreamingResponse:
    """查询请假进度。"""
    return await _guided_flow_stream(user, "", session_id, trace_id,
                                     leave_events, container, kind="leave", start=False)


async def _reimburse_stream(user, question: str, session_id: str, trace_id: str,
                            reimburse_events: list, container) -> StreamingResponse:
    """报销引导式对话。"""
    return await _guided_flow_stream(user, question, session_id, trace_id,
                                     reimburse_events, container, kind="reimburse", start=True)


async def _reimburse_progress_stream(user, session_id: str, trace_id: str,
                                     reimburse_events: list, container) -> StreamingResponse:
    """查询报销进度。"""
    return await _guided_flow_stream(user, "", session_id, trace_id,
                                     reimburse_events, container, kind="reimburse", start=False)


def _stage_label(node: str) -> str:
    return {
        "classify_intent": "正在理解您的问题",
        "permission_gate": "正在校验访问权限",
        "retrieve": "正在检索知识库",
        "generate": "正在生成回答",
        "deny": "权限校验未通过",
        "handle_error": "处理出现异常",
    }.get(node, node)


# ============================================================
#  会话 / 反馈 / 热门
# ============================================================
@router.get("/sessions", summary="会话列表（仅当前账号）")
async def list_sessions(user=Depends(current_user)):
    return store.list_sessions(user.user_id)


@router.post("/sessions", summary="新建会话")
async def new_session(user=Depends(current_user)):
    s = store.create_session(user.user_id)
    return {"id": s["id"], "title": s["title"], "turns": 0,
            "created_at": s["created_at"], "updated_at": s["updated_at"]}


@router.get("/sessions/{session_id}", summary="会话详情（含历史消息）")
async def get_session_detail(session_id: str, user=Depends(current_user)):
    s = store.get_session(user.user_id, session_id)
    if s is None:
        raise HTTPException(status_code=404, detail="会话不存在或不属于当前账号")
    return s


@router.patch("/sessions/{session_id}", summary="重命名会话")
async def rename_session(session_id: str, body: dict, user=Depends(current_user)):
    title = str(body.get("title", "")).strip()
    if not title:
        raise HTTPException(status_code=400, detail="title 不能为空")
    if not store.rename_session(user.user_id, session_id, title):
        raise HTTPException(status_code=404, detail="会话不存在或不属于当前账号")
    return {"ok": True, "title": title[:40]}


@router.post("/sessions/pin-order", summary="置顶会话拖拽排序（按传入 id 顺序持久化）")
async def pin_order(body: dict, user=Depends(current_user)):
    ids = [str(x) for x in (body.get("ids") or [])]
    n = store.reorder_pinned(user.user_id, ids)
    return {"ok": True, "updated": n}


@router.post("/sessions/{session_id}/pin", summary="置顶 / 取消置顶会话")
async def pin_session(session_id: str, body: dict, user=Depends(current_user)):
    pinned = bool(body.get("pinned"))
    if not store.pin_session(user.user_id, session_id, pinned):
        raise HTTPException(status_code=404, detail="会话不存在或不属于当前账号")
    return {"ok": True, "pinned": pinned}


@router.delete("/sessions/{session_id}", summary="删除会话")
async def delete_session(session_id: str, user=Depends(current_user)):
    if not store.delete_session(user.user_id, session_id):
        raise HTTPException(status_code=404, detail="会话不存在或不属于当前账号")
    return {"ok": True}


@router.post("/feedback", summary="满意度反馈 👍/👎")
async def feedback(body: FeedbackRequest, user=Depends(active_user)):
    feedback_store.record_feedback({
        **body.model_dump(), "user_id": user.user_id,
        "department_id": user.dept_id,
    })
    return {"ok": True}


@router.get("/hot-questions", summary="热门问题排行榜")
async def hot_questions(top: int = 10, user=Depends(active_user)):
    return feedback_store.hot_questions(top, department_id=user.dept_id)


@router.get("/feedback-stats", summary="满意度统计")
async def feedback_stats(user=Depends(current_user)):
    return feedback_store.feedback_stats()
