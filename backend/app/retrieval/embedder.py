"""
Embedding 适配器 —— 三种后端，用配置切换，上层零感知。

  local  : sentence-transformers 加载本地 BGE-large-zh-v1.5（1024维，私有化主选）
  http   : 调用内网独立的 embedding 微服务
  hash   : 离线哈希向量（无任何模型依赖，用汉字 bigram 的 hashing trick）

为什么保留 hash 模式：
  客户 POC 环境常常 GPU 还没到货、模型还没导入，但需要先把链路跑通给领导演示。
  hash 模式用汉字二元组做哈希投影，对中文仍有可观的语义相似度（共享词越多越近），
  足够验证"混合检索 + RRF + 权限过滤 + 流式输出"整条链路是否通畅。
"""
from __future__ import annotations

import hashlib
import re
from functools import lru_cache

import numpy as np

from ..config import settings
from ..logging_setup import get_logger

log = get_logger("embedder")

_WORD = re.compile(r"[a-zA-Z0-9]+|[\u4e00-\u9fff]")


def _tokenize(text: str) -> list[str]:
    """中文按字、英文按词，再补一层二元组（近似 bigram 语义）。"""
    units = _WORD.findall(text.lower())
    grams = list(units)
    grams += [f"{units[i]}{units[i+1]}" for i in range(len(units) - 1)]
    return grams


def hash_embed(text: str, dim: int | None = None) -> np.ndarray:
    """Hashing trick：把 n-gram 投影到固定维度并做 L2 归一化。"""
    dim = dim or settings.EMBEDDING_DIM
    vec = np.zeros(dim, dtype=np.float32)
    for g in _tokenize(text):
        h = int(hashlib.md5(g.encode("utf-8")).hexdigest()[:8], 16)
        vec[h % dim] += 1.0
    norm = np.linalg.norm(vec)
    if norm > 0:
        vec /= norm
    return vec


class Embedder:
    def __init__(self):
        self.backend = settings.EMBEDDING_BACKEND
        self.dim = settings.EMBEDDING_DIM
        self._model = None
        if self.backend == "local":
            self._load_local()
        log.info("embedder.init", backend=self.backend, dim=self.dim)

    def _load_local(self):
        try:
            from sentence_transformers import SentenceTransformer

            self._model = SentenceTransformer(settings.EMBEDDING_MODEL_PATH)
            self.dim = self._model.get_sentence_embedding_dimension() or self.dim
            log.info("embedder.local_loaded", path=settings.EMBEDDING_MODEL_PATH)
        except Exception as e:  # noqa: BLE001
            log.warning("embedder.local_unavailable_fallback_hash", error=str(e))
            self.backend = "hash"

    async def aembed(self, text: str) -> np.ndarray:
        if self.backend == "local" and self._model is not None:
            import asyncio

            return await asyncio.to_thread(lambda: np.asarray(self._model.encode(text), dtype=np.float32))

        if self.backend == "http":
            return await self._embed_http(text)

        return hash_embed(text, self.dim)

    async def _embed_http(self, text: str) -> np.ndarray:
        import httpx

        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(settings.EMBEDDING_HTTP_URL, json={"texts": [text]})
            r.raise_for_status()
            data = r.json()
            return np.asarray(data["embeddings"][0], dtype=np.float32)

    def embed_sync(self, text: str) -> np.ndarray:
        if self.backend == "local" and self._model is not None:
            return np.asarray(self._model.encode(text), dtype=np.float32)
        return hash_embed(text, self.dim)


@lru_cache(maxsize=1)
def get_embedder() -> Embedder:
    return Embedder()
