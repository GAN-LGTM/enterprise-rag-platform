"""
向量存储 —— 统一 doc_chunks 表 + department_id 逻辑隔离（需求书 FR-KB-01）。

两套实现：
  PGVectorStore     : PostgreSQL 15+ / pgvector（生产）
  MemoryVectorStore : 内存 + numpy（PG 不可用时的降级，保证服务不中断）

关键点：所有查询方法都强制要求 dept_ids 参数（assert_dept_ids），
漏传直接抛异常 —— 这是三层防护的第一层。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import numpy as np

from ..config import settings
from ..logging_setup import get_logger
from ..perm.policy import assert_dept_ids

log = get_logger("store")


@dataclass
class Chunk:
    id: int | None
    department_id: str
    chunk_text: str
    doc_name: str
    page_num: int | None
    chunk_index: int
    version: int = 1
    score: float = 0.0
    metadata: dict | None = None

    def to_dict(self):
        return {
            "id": self.id, "department_id": self.department_id, "text": self.chunk_text,
            "doc_name": self.doc_name, "page_num": self.page_num, "score": round(self.score, 4),
        }


class VectorStore(Protocol):
    async def vector_search(self, embedding: np.ndarray, dept_ids: list[str], top_k: int) -> list[Chunk]: ...
    async def fts_search(self, query: str, dept_ids: list[str], top_k: int) -> list[Chunk]: ...
    async def add_chunks(self, chunks: list[Chunk], embeddings: list[np.ndarray]) -> int: ...
    async def delete_doc(self, doc_name: str, department_id: str) -> int: ...
    async def health(self) -> tuple[bool, str]: ...


# ============================================================
#  PostgreSQL + pgvector
# ============================================================
class PGVectorStore:
    """HNSW 向量索引 + GIN 全文索引 + department_id B-tree 索引。"""

    def __init__(self, dsn: str | None = None):
        self.dsn = dsn or settings.DATABASE_URL
        self._engine = None

    def _get_engine(self):
        if self._engine is None:
            from sqlalchemy import create_engine

            self._engine = create_engine(
                self.dsn, pool_size=settings.DB_POOL_SIZE, max_overflow=settings.DB_MAX_OVERFLOW,
                pool_pre_ping=True, future=True,
            )
        return self._engine

    async def vector_search(self, embedding: np.ndarray, dept_ids: list[str], top_k: int) -> list[Chunk]:
        import asyncio
        from sqlalchemy import text

        dept_ids = assert_dept_ids(dept_ids)
        if not dept_ids:
            return []

        sql = text("""
            SELECT id, department_id, chunk_text, doc_name, page_num, chunk_index, doc_version,
                   1 - (embedding <=> :emb) AS score
            FROM doc_chunks
            WHERE department_id = ANY(:depts)
            ORDER BY embedding <=> :emb
            LIMIT :k
        """)

        def _run():
            with self._get_engine().connect() as conn:
                rows = conn.execute(
                    sql, {"emb": embedding.tolist(), "depts": dept_ids, "k": top_k}
                ).mappings().all()
            return [Chunk(
                id=r["id"], department_id=r["department_id"], chunk_text=r["chunk_text"],
                doc_name=r["doc_name"], page_num=r["page_num"], chunk_index=r["chunk_index"],
                version=r["doc_version"] or 1, score=float(r["score"] or 0.0),
            ) for r in rows]

        return await asyncio.to_thread(_run)

    async def fts_search(self, query: str, dept_ids: list[str], top_k: int) -> list[Chunk]:
        """
        全文检索。中文分词依赖 zhparser / pg_jieba 扩展；
        未安装时自动降级为 ILIKE 匹配，保证功能不缺失（只是精度略降）。
        """
        import asyncio
        from sqlalchemy import text

        dept_ids = assert_dept_ids(dept_ids)
        if not dept_ids:
            return []

        sql = text("""
            SELECT id, department_id, chunk_text, doc_name, page_num, chunk_index, doc_version,
                   ts_rank(fts_document, plainto_tsquery('simple', :q)) AS score
            FROM doc_chunks
            WHERE department_id = ANY(:depts)
              AND (fts_document @@ plainto_tsquery('simple', :q) OR chunk_text ILIKE :like)
            ORDER BY score DESC
            LIMIT :k
        """)

        def _run():
            with self._get_engine().connect() as conn:
                rows = conn.execute(
                    sql, {"q": query, "depts": dept_ids, "k": top_k,
                          "like": f"%{query[:20]}%"}
                ).mappings().all()
            return [Chunk(
                id=r["id"], department_id=r["department_id"], chunk_text=r["chunk_text"],
                doc_name=r["doc_name"], page_num=r["page_num"], chunk_index=r["chunk_index"],
                version=r["doc_version"] or 1, score=float(r["score"] or 0.0),
            ) for r in rows]

        return await asyncio.to_thread(_run)

    async def add_chunks(self, chunks: list[Chunk], embeddings: list[np.ndarray]) -> int:
        import asyncio
        from sqlalchemy import text

        def _run():
            sql = text("""
                INSERT INTO doc_chunks
                    (department_id, chunk_text, embedding, doc_name, doc_version,
                     page_num, chunk_index, metadata, uploaded_by)
                VALUES (:department_id, :chunk_text, :embedding, :doc_name, :version,
                        :page_num, :chunk_index, :metadata, :uploaded_by)
            """)
            payload = [{
                "department_id": c.department_id, "chunk_text": c.chunk_text,
                "embedding": e.tolist(), "doc_name": c.doc_name, "version": c.version,
                "page_num": c.page_num, "chunk_index": c.chunk_index,
                "metadata": c.metadata or {}, "uploaded_by": "system",
            } for c, e in zip(chunks, embeddings)]
            with self._get_engine().begin() as conn:
                conn.execute(sql, payload)
            return len(payload)

        return await asyncio.to_thread(_run)

    async def delete_doc(self, doc_name: str, department_id: str) -> int:
        import asyncio
        from sqlalchemy import text

        def _run():
            with self._get_engine().begin() as conn:
                res = conn.execute(
                    text("DELETE FROM doc_chunks WHERE doc_name=:d AND department_id=:p"),
                    {"d": doc_name, "p": department_id},
                )
                return res.rowcount or 0

        return await asyncio.to_thread(_run)

    async def health(self) -> tuple[bool, str]:
        import asyncio
        from sqlalchemy import text

        try:
            def _run():
                with self._get_engine().connect() as conn:
                    conn.execute(text("SELECT 1"))
                    conn.execute(text("SELECT 'pgvector' FROM pg_extension WHERE extname='vector'"))
                return True

            await asyncio.to_thread(_run)
            return True, "PostgreSQL + pgvector 正常"
        except Exception as e:  # noqa: BLE001
            return False, f"PostgreSQL 不可达: {type(e).__name__}"


# ============================================================
#  内存降级实现
# ============================================================
class MemoryVectorStore:
    """PG 不可用时的兜底，保证问答主链路不中断（需求书 NFR-USA-02/03 的降级思想）。"""

    def __init__(self):
        self._chunks: list[Chunk] = []
        self._vectors: list[np.ndarray] = []
        self._next_id = 1

    async def vector_search(self, embedding: np.ndarray, dept_ids: list[str], top_k: int) -> list[Chunk]:
        dept_ids = assert_dept_ids(dept_ids)
        if not self._vectors or not dept_ids:
            return []
        mat = np.vstack(self._vectors)
        sims = mat @ embedding  # 已归一化 → 点积即余弦
        allowed = {d for d in dept_ids}
        idx = [i for i, c in enumerate(self._chunks) if c.department_id in allowed]
        if not idx:
            return []
        order = sorted(idx, key=lambda i: -sims[i])[:top_k]
        out = []
        for i in order:
            c = self._chunks[i]
            out.append(Chunk(c.id, c.department_id, c.chunk_text, c.doc_name, c.page_num,
                             c.chunk_index, c.version, float(sims[i])))
        return out

    async def fts_search(self, query: str, dept_ids: list[str], top_k: int) -> list[Chunk]:
        dept_ids = assert_dept_ids(dept_ids)
        allowed = {d for d in dept_ids}
        keys = _tokenize_for_fts(query)
        scored = []
        for c in self._chunks:
            if c.department_id not in allowed:
                continue
            text_lower = c.chunk_text.lower()
            hits = sum(1 for k in keys if k in text_lower)
            if hits:
                scored.append((hits, c))
        scored.sort(key=lambda x: -x[0])
        return [Chunk(c.id, c.department_id, c.chunk_text, c.doc_name, c.page_num,
                      c.chunk_index, c.version, float(h / max(len(keys), 1)))
                for h, c in scored[:top_k]]

    async def add_chunks(self, chunks: list[Chunk], embeddings: list[np.ndarray]) -> int:
        for c, e in zip(chunks, embeddings):
            c.id = self._next_id
            self._next_id += 1
            self._chunks.append(c)
            self._vectors.append(e)
        return len(chunks)

    async def delete_doc(self, doc_name: str, department_id: str) -> int:
        keep = [(c, v) for c, v in zip(self._chunks, self._vectors)
                if not (c.doc_name == doc_name and c.department_id == department_id)]
        removed = len(self._chunks) - len(keep)
        if keep:
            self._chunks, self._vectors = [x[0] for x in keep], [x[1] for x in keep]
        else:
            self._chunks, self._vectors = [], []
        return removed

    async def health(self) -> tuple[bool, str]:
        return True, f"内存向量库（降级模式），共 {len(self._chunks)} 个片段"


def _tokenize_for_fts(query: str) -> list[str]:
    import re

    units = re.findall(r"[a-zA-Z0-9]+|[\u4e00-\u9fff]", query.lower())
    grams = list(units)
    grams += [f"{units[i]}{units[i+1]}" for i in range(len(units) - 1)]
    return list(dict.fromkeys(grams))
