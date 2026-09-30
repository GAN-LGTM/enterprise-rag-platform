"""Prometheus 指标。/metrics 端点暴露，Grafana 直接抓取。"""
from __future__ import annotations

from .config import settings

try:
    from prometheus_client import Counter, Gauge, Histogram, generate_latest, CONTENT_TYPE_LATEST
    _OK = True
except Exception:  # 未安装指标库时静默降级，不影响主流程
    _OK = False


if _OK:
    REQUESTS = Counter("rag_requests_total", "总请求数", ["endpoint", "status"])
    LATENCY = Histogram("rag_request_latency_seconds", "请求耗时", ["endpoint"])
    NODE_LATENCY = Histogram("rag_node_latency_seconds", "LangGraph 节点耗时", ["node"])
    INTENT_COUNT = Counter("rag_intent_total", "意图分布", ["intent"])
    CACHE_HITS = Counter("rag_cache_total", "缓存命中情况", ["result"])
    PERM_DENIED = Counter("rag_permission_denied_total", "权限拦截次数", ["reason"])
    LLM_CALLS = Counter("rag_llm_calls_total", "LLM 调用", ["model", "status"])
    LLM_TOKENS = Counter("rag_llm_tokens_total", "生成 token 数", ["model"])
    STREAM_ACTIVE = Gauge("rag_stream_active", "当前活跃 SSE 流数量")
else:  # 占位实现，接口保持一致
    class _Noop:
        def labels(self, *a, **k):
            return self

        def inc(self, *a, **k):
            pass

        def dec(self, *a, **k):
            pass

        def observe(self, *a, **k):
            pass

        def set(self, *a, **k):
            pass

    REQUESTS = LATENCY = NODE_LATENCY = INTENT_COUNT = CACHE_HITS = _Noop()
    PERM_DENIED = LLM_CALLS = LLM_TOKENS = STREAM_ACTIVE = _Noop()


def metrics_response():
    if not (_OK and settings.METRICS_ENABLED):
        return None
    from fastapi import Response

    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
