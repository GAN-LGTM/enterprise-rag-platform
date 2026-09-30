"""
应用容器 —— 集中装配所有组件，并处理"组件不可用时的降级"。

私有化交付时常见场景：客户环境 GPU 还没到位、Redis 还没装、PG 还在申请。
容器保证这些情况下服务依然能启动（降级模式），而不是直接 500。
"""
from __future__ import annotations

from .cache.cache import CacheBackend, get_cache
from .config import settings
from .intent.classifier import IntentClassifier
from .llm.manager import LLMManager
from .llm.mock_backend import MockBackend
from .llm.openai_compat_backend import OpenAICompatBackend
from .logging_setup import get_logger
from .retrieval.embedder import Embedder, get_embedder
from .retrieval.store import MemoryVectorStore, PGVectorStore, VectorStore

log = get_logger("container")


class Container:
    def __init__(self):
        # ---- 存储：默认 PG，init() 时探测，不通则降级内存 ----
        self.store: VectorStore = PGVectorStore()
        self.store_mode = "pg"

        # ---- 其他组件（均为惰性初始化，不阻塞启动）----
        self.embedder: Embedder = get_embedder()
        self.cache: CacheBackend = get_cache()
        self.llm = self._build_llm()
        self.classifier = IntentClassifier(llm_manager=self.llm)
        self.workflow = None  # type: ignore[assignment]

    async def init(self) -> "Container":
        """
        异步探测存储可用性并决定是否降级。
        必须在事件循环里调用（lifespan 中），这样 PG 探测才能真正发起连接。
        """
        import asyncio

        if settings.DB_FORCE_MEMORY:
            self.store, self.store_mode = MemoryVectorStore(), "memory"
        elif settings.DB_AUTO_FALLBACK and isinstance(self.store, PGVectorStore):
            try:
                ok, msg = await asyncio.wait_for(self.store.health(), timeout=3.0)
                if not ok:
                    raise RuntimeError(msg)
                log.info("container.pg_ready")
            except Exception as e:  # noqa: BLE001
                log.warning("container.pg_fallback", reason=str(e))
                self.store, self.store_mode = MemoryVectorStore(), "memory"
        else:
            self.store_mode = "pg"

        # 存储确定后再编译工作流，保证节点拿到正确的 store 引用
        from .graph.workflow import RAGWorkflow

        self.workflow = RAGWorkflow(self)

        log.info("container.ready", store=self.store_mode, embedding=self.embedder.backend,
                 llm=self.llm.primary.name, cache=self.cache.mode, graph=self.workflow.engine)
        return self

    def _build_llm(self) -> LLMManager:
        if settings.LLM_BACKEND == "cloud":
            # 云端免密钥模型（演示/无 GPU 环境的真实对话），失败降级 mock 话术
            from .llm.cloud_backend import CloudLLMBackend

            return LLMManager(CloudLLMBackend(), MockBackend())
        if settings.LLM_BACKEND == "vllm":
            primary = OpenAICompatBackend()
            fallback = OpenAICompatBackend(model=settings.LLM_FALLBACK_MODEL)
            return LLMManager(primary, fallback)
        # mock：无 GPU 也能跑通全链路；同时把 mock 当作兜底后端
        primary = MockBackend()
        fallback = MockBackend()
        fallback.model = "mock-fallback"
        return LLMManager(primary, fallback)


_container: Container | None = None


def get_container() -> Container:
    global _container
    if _container is None:
        _container = Container()
    return _container
