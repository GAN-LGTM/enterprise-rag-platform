"""
LangGraph 工作流装配 + 流式执行。

图结构：
    START → classify_intent ─┬─→ permission_gate ─┬─→ retrieve → generate → END
                             │                    └─→ deny ─────────────→ END
                             │                    └─→ handle_error ─────→ END
                             ├─(chitchat/operation，无时效性)→ generate → END
                             └─(带时效性的闲聊)→ retrieve(外部工具) → generate → END

为什么用 LangGraph 而不是 if-else：
  6 类意图、多个节点的条件跳转，if-else 会迅速腐化成面条代码。
  用图之后，新增一个意图 = 加一个节点 + 在路由表里加一行，不用动已有节点。

流式怎么和图共存（关键设计）：
  generate 节点把 LLM 的 async generator 放进 State["token_stream"]，
  图的 astream 负责输出"节点级事件"（stage），API 层再消费 token_stream 输出"字级事件"。
  这样既有编排能力，又能逐字推送给前端。

降级：langgraph 未安装 / API 不兼容时，自动切换到内置的 MiniGraph，
      节点与路由的定义完全不用改。
"""
from __future__ import annotations

import time
from collections.abc import AsyncIterator, Callable

from ..config import settings
from ..logging_setup import get_logger
from .nodes import build_nodes
from .state import GraphState

log = get_logger("graph.workflow")

END = "__end__"


# ============================================================
#  路由函数（条件边）
# ============================================================
def route_after_intent(state: GraphState) -> str:
    """意图识别后的分流：闲聊/操作型不进权限门和检索，直接生成。

    例外：闲聊里带时效性的（"今天天气""最新新闻"）仍要走 retrieve 节点
    调用天气/联网搜索外部工具——这是它们唯一的数据来源。
    """
    if state.get("error"):
        return "handle_error"
    if state.get("intent") in ("chitchat", "operation"):
        if settings.PUBLIC_SEARCH_ENABLED:
            from ..tools import realtime, websearch

            q = state.get("question", "")
            # 外部工具统一判定：天气 / 行情各自在注册表里声明自己的触发规则
            if (realtime.is_external_query(q) or websearch.is_search_query(q)
                    or websearch.is_hard_realtime(q)):
                return "retrieve"
        return "generate"
    return "permission_gate"


def route_after_permission(state: GraphState) -> str:
    if state.get("error"):
        return "handle_error"
    decision = state.get("decision")
    if decision is None or not decision.allowed:
        return "deny"
    return "retrieve"


def route_after_retrieve(state: GraphState) -> str:
    if state.get("error"):
        return "handle_error"
    return "generate"


# ============================================================
#  内置 MiniGraph（LangGraph 不可用时的等价实现）
# ============================================================
class MiniGraph:
    """轻量状态机，接口与 LangGraph 的 astream(stream_mode='updates') 保持一致。"""

    def __init__(self, nodes: dict[str, Callable], edges: dict[str, str | Callable], start: str):
        self.nodes = nodes
        self.edges = edges
        self.start = start

    async def astream(self, state: GraphState, stream_mode: str = "updates"):
        cur = self.start
        hops = 0
        while cur and cur != END and hops < 32:
            hops += 1
            out = await self.nodes[cur](state)
            state.update(out)
            yield {cur: out}
            nxt = self.edges.get(cur, END)
            cur = nxt(state) if callable(nxt) else nxt


# ============================================================
#  工作流
# ============================================================
class RAGWorkflow:
    def __init__(self, container):
        self.container = container
        self.nodes = build_nodes(container)
        self.engine = "minigraph"
        self.graph = self._build()

    # ---------------- 装配 ----------------
    def _build(self):
        # 给每个节点套一层异常捕获：任何节点抛异常都转入 handle_error，不让整个图崩掉
        wrapped = {name: self._safe(name, fn) for name, fn in self.nodes.items()}

        try:
            from langgraph.graph import END as LG_END  # noqa: F401
            from langgraph.graph import START, StateGraph

            g = StateGraph(GraphState)
            for name, fn in wrapped.items():
                g.add_node(name, fn)

            g.add_edge(START, "classify_intent")
            g.add_conditional_edges(
                "classify_intent", route_after_intent,
                {"permission_gate": "permission_gate", "retrieve": "retrieve",
                 "generate": "generate", "handle_error": "handle_error"},
            )
            g.add_conditional_edges(
                "permission_gate", route_after_permission,
                {"retrieve": "retrieve", "deny": "deny", "handle_error": "handle_error"},
            )
            g.add_conditional_edges(
                "retrieve", route_after_retrieve,
                {"generate": "generate", "handle_error": "handle_error"},
            )
            g.add_edge("generate", LG_END)
            g.add_edge("deny", LG_END)
            g.add_edge("handle_error", LG_END)

            self.engine = "langgraph"
            log.info("graph.engine", engine="langgraph")
            return g.compile()

        except Exception as e:  # noqa: BLE001
            log.warning("graph.langgraph_unavailable", error=str(e), fallback="MiniGraph")
            self.engine = "minigraph"
            return MiniGraph(
                nodes=wrapped,
                edges={
                    "classify_intent": route_after_intent,
                    "permission_gate": route_after_permission,
                    "retrieve": route_after_retrieve,
                    "generate": END,
                    "deny": END,
                    "handle_error": END,
                },
                start="classify_intent",
            )

    def _safe(self, name: str, fn: Callable) -> Callable:
        async def _wrapped(state: GraphState):
            t0 = time.perf_counter()
            try:
                out = await fn(state)
                state.setdefault("timings", {})[name] = round((time.perf_counter() - t0) * 1000, 2)
                return out
            except Exception as e:  # noqa: BLE001
                cost = round((time.perf_counter() - t0) * 1000, 2)
                log.error("graph.node_exception", node=name, error=str(e), cost_ms=cost)
                state.setdefault("timings", {})[name] = cost
                return {
                    "error": f"{type(e).__name__}: {e}",
                    "error_code": getattr(e, "code", "NODE_ERROR"),
                    "user_message": getattr(e, "user_message", None) or "处理过程中出现异常，请稍后重试。",
                    "stage": name,
                }

        return _wrapped

    # ---------------- 流式执行 ----------------
    async def run_stream(self, state: GraphState) -> AsyncIterator[tuple[str, str, dict]]:
        """
        产出两类事件：
          ("stage", node_name, node_output)  —— 节点级进度
          ("token", text, {})                —— 字级增量

        API 层拿到后分别包装成 SSE 的 stage / token 事件。
        """
        t0 = time.perf_counter()
        merged: dict = {}
        # generate 节点返回的是惰性 token 流：节点包装器记到的耗时只有"构造流"的开销，
        # 真正的 LLM 生成发生在这里消费流的过程中。必须把这段时间补计回 generate，
        # 否则节点耗时之和远小于 __total__，可观测性数据自相矛盾。
        stream_cost_ms = 0.0
        try:
            if self.engine == "langgraph":
                async for update in self.graph.astream(state, stream_mode="updates"):
                    for node_name, out in update.items():
                        out = out or {}
                        merged.update(out)
                        yield ("stage", node_name, out)
                        stream = out.get("token_stream")
                        if stream is not None:
                            s0 = time.perf_counter()
                            async for tok in stream:
                                yield ("token", tok, {})
                            stream_cost_ms += (time.perf_counter() - s0) * 1000
            else:
                async for update in self.graph.astream(state):
                    for node_name, out in update.items():
                        out = out or {}
                        merged.update(out)
                        yield ("stage", node_name, out)
                        stream = out.get("token_stream")
                        if stream is not None:
                            s0 = time.perf_counter()
                            async for tok in stream:
                                yield ("token", tok, {})
                            stream_cost_ms += (time.perf_counter() - s0) * 1000
        except Exception as e:  # noqa: BLE001
            log.error("graph.stream_exception", error=str(e))
            yield ("stage", "handle_error", {"error": str(e),
                                             "user_message": "服务处理异常，请稍后重试。"})
        finally:
            # 把各节点的输出回写到调用方持有的 state。
            # LangGraph 在节点间传的是 state 副本，调用方如果不回写，
            # 就拿不到 citations / intent / dept_ids 这些"替换型"字段。
            merged.pop("token_stream", None)
            state.update(merged)
            timings = state.setdefault("timings", {})
            if stream_cost_ms > 0:
                # 把流式生成耗时并入 generate，使"各节点耗时之和 ≈ __total__"
                timings["generate"] = round(timings.get("generate", 0.0) + stream_cost_ms, 2)
            timings["__total__"] = round((time.perf_counter() - t0) * 1000, 2)
