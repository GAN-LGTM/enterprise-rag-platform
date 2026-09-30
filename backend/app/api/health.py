"""健康检查：DB / Redis / LLM / Embedding 各组件状态，供容器平台与巡检任务使用。"""
from __future__ import annotations

import time

from fastapi import APIRouter, Request

from ..schemas.dto import HealthComponent, HealthResponse

router = APIRouter(prefix="/api", tags=["运维"])


@router.get("/health", response_model=HealthResponse, summary="各组件健康状态")
async def health(request: Request):
    from ..config import settings

    container = request.app.state.container
    components: list[HealthComponent] = []

    async def probe(name: str, coro) -> HealthComponent:
        t0 = time.perf_counter()
        try:
            ok, msg = await coro
            return HealthComponent(name=name, status="ok" if ok else "down", detail=msg,
                                   latency_ms=round((time.perf_counter() - t0) * 1000, 2))
        except Exception as e:  # noqa: BLE001
            return HealthComponent(name=name, status="down", detail=f"{type(e).__name__}: {e}",
                                   latency_ms=round((time.perf_counter() - t0) * 1000, 2))

    components.append(await probe("database", container.store.health()))
    components.append(await probe("cache", container.cache.health()))
    components.append(await probe("embedding", _embed_health(container)))
    components.append(await probe("llm", _llm_health(container)))

    # ---- 状态判定：每一条降级都必须能说清原因，否则宁可显示 ok ----
    notes: list[str] = []
    for c in components:
        if c.status == "down":
            notes.append(f"组件 {c.name} 不可用：{c.detail}")

    # 基础设施降级（组件本身能跑，但用的是兜底实现）
    if getattr(container, "store_mode", "") == "memory":
        notes.append("向量存储为内存模式（未连接 Milvus/Qdrant）：检索可用，但重启后索引需重建，生产环境请连接外部向量库")
    if getattr(container.cache, "mode", "") == "memory":
        notes.append("缓存为本地内存模式（未连接 Redis）：多实例部署时缓存不共享，生产环境请连接 Redis")

    # LLM 响应慢：单独提示，不判 down，但记为降级原因
    llm_comp = next((c for c in components if c.name == "llm"), None)
    if llm_comp and llm_comp.status == "ok" and llm_comp.latency_ms > settings.LLM_SLOW_MS:
        notes.append(f"LLM 响应较慢（{llm_comp.latency_ms:.0f}ms，阈值 {settings.LLM_SLOW_MS:.0f}ms）："
                     f"首字超时保护已就绪，必要时自动切换备用模型 {settings.LLM_FALLBACK_MODEL}")

    any_down = any(c.status == "down" for c in components)
    if any_down:
        overall = "down"
    elif notes:
        overall = "degraded"
    else:
        overall = "ok"

    # ---- 模型清单（私有化交付验收常问）----
    models = {
        "llm": {
            "backend": settings.LLM_BACKEND,
            "model": settings.CLOUD_LLM_MODEL if settings.LLM_BACKEND == "cloud" else settings.LLM_MODEL,
            "fallback": settings.LLM_FALLBACK_MODEL,
            "endpoint": settings.CLOUD_LLM_ENDPOINT if settings.LLM_BACKEND == "cloud" else settings.LLM_BASE_URL,
        },
        "embedding": {
            "backend": settings.EMBEDDING_BACKEND,
            "model": settings.EMBEDDING_MODEL_PATH,
            "dim": settings.EMBEDDING_DIM,
        },
        "intent": {"model": settings.BERT_MODEL_PATH},
        "ocr": {"enabled": settings.OCR_ENABLED, "engine": "PaddleOCR" if settings.OCR_ENABLED else "未启用"},
    }

    return HealthResponse(status=overall, version=settings.APP_VERSION,
                          components=components, notes=notes, models=models)


async def _embed_health(container):
    import asyncio

    try:
        vec = await asyncio.wait_for(container.embedder.aembed("健康检查"), timeout=5.0)
        return True, f"Embedding 正常（{container.embedder.backend}, dim={len(vec)}）"
    except Exception as e:  # noqa: BLE001
        return False, f"Embedding 异常: {type(e).__name__}"


async def _llm_health(container):
    from ..llm.base import LLMMessage

    try:
        import asyncio

        async def _once():
            async for _ in container.llm.astream([LLMMessage(role="user", content="ping")]):
                return True
            return False

        ok = await asyncio.wait_for(_once(), timeout=8.0)
        return ok, f"LLM 正常（{container.llm.primary.name}）" if ok else "LLM 返回空流"
    except Exception as e:  # noqa: BLE001
        return False, f"LLM 不可用: {type(e).__name__}"


@router.get("/ping", summary="存活探针")
async def ping():
    return {"pong": True}
